#!/usr/bin/env python3
"""口播离线后期；Python 3.9 标准库 + 系统 ffmpeg/ffprobe。"""
import argparse
from array import array
import csv
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile

SR = 48000
HOP = 480
FADE = 480
SILENCE_FILTER = "silencedetect=noise=-40dB:d=0.15"
EQ = "highpass=f=80,equalizer=f=300:t=q:w=1:g=-2,equalizer=f=3000:t=q:w=1:g=2"
DEESS = "deesser=i=0.2:m=0.3:f=0.5"
COMP = "acompressor=threshold=-20dB:ratio=2:attack=10:release=150"


class AudioError(Exception):
    pass


def run(argv):
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=240)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError("无法运行工具或运行超时：{}".format(argv[0])) from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace")[-1800:]
        raise AudioError("{} 执行失败：{}".format(argv[0], detail))
    return result


def ffmpeg(args):
    return run(["ffmpeg", "-hide_banner", "-nostdin", "-y"] + args)


def input_args(path):
    if Path(path).suffix == ".f32":
        return ["-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(path)]
    return ["-i", str(path)]


def probe(path):
    result = run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                  "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
                  "-of", "json", str(path)])
    try:
        info = json.loads(result.stdout)
        if not info.get("streams"):
            raise ValueError("没有音轨")
        return info
    except (ValueError, KeyError) as exc:
        raise AudioError("不是有效音频：{}".format(path)) from exc


def decode(path):
    if not path.is_file() or path.stat().st_size == 0:
        raise AudioError("输入不存在或为空：{}".format(path))
    info = probe(path)
    data = ffmpeg(["-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1",
                   "-ar", str(SR), "-f", "f32le", "-"]).stdout
    samples = array("f")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples or any(not math.isfinite(v) for v in samples):
        raise AudioError("音频为空或含无效采样：{}".format(path))
    if max(abs(v) for v in samples) == 0:
        raise AudioError("输入全静音（包括下混相消）：{}".format(path))
    return samples, info


def save_raw(path, samples):
    if sys.byteorder == "little":
        with open(path, "wb") as stream:
            samples.tofile(stream)
    else:
        copy = array("f", samples)
        copy.byteswap()
        with open(path, "wb") as stream:
            copy.tofile(stream)


def loud_json(log):
    # loudnorm 的对象没有嵌套；不依赖日志总行数、前后其他工具消息。
    for block in reversed(re.findall(r"\{[^{}]*\}", log)):
        try:
            value = json.loads(block)
            keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
            if all(key in value for key in keys):
                if not all(math.isfinite(float(value[key])) for key in keys):
                    raise AudioError("无法测得有限响度（全静音、太短或电平太低）")
                return value
        except (ValueError, TypeError):
            continue
    raise AudioError("ffmpeg 未返回有效 loudnorm 测量 JSON")


def loud_filter(target):
    return "loudnorm=I={}:TP=-2.0:LRA=11".format(target)


def measure(path, target=-16):
    result = ffmpeg(input_args(path) + ["-map", "0:a:0", "-af",
                    loud_filter(target) + ":print_format=json", "-f", "null", "-"])
    return loud_json(result.stderr.decode("utf-8", "replace"))


def pauses(path, duration):
    result = ffmpeg(input_args(path) + ["-map", "0:a:0", "-af", SILENCE_FILTER,
                                      "-f", "null", "-"])
    log = result.stderr.decode("utf-8", "replace")
    intervals = []
    start = None
    for event in re.finditer(r"silence_(start|end):\s*([-+\d.eE]+)", log):
        kind, value = event.group(1), float(event.group(2))
        if kind == "start":
            start = value
        elif start is not None:
            # 不靠列表切片猜首尾：明确排除接触文件边界的静音。
            if start > 1.0 / SR and value < duration - 1.0 / SR:
                intervals.append([start, value])
            start = None
    lengths = [b - a for a, b in intervals]
    return {"count": len(lengths), "median": statistics.median(lengths) if lengths else 0.0,
            "max": max(lengths, default=0.0), "intervals": intervals,
            "filter": SILENCE_FILTER, "exclude_edges": True}


def percentile(values, fraction):
    ordered = sorted(values)
    pos = (len(ordered) - 1) * fraction
    left = int(pos)
    right = min(left + 1, len(ordered) - 1)
    return ordered[left] + (ordered[right] - ordered[left]) * (pos - left)


def trim(samples):
    threshold = max(abs(v) for v in samples) * 10 ** (-50 / 20)
    first = next(i for i, v in enumerate(samples) if abs(v) > threshold)
    last = next(i for i in range(len(samples) - 1, -1, -1) if abs(samples[i]) > threshold)
    start = max(0, first - round(.03 * SR))
    end = min(len(samples), last + round(.12 * SR))
    return samples[start:end], start, end


def compress_pauses(samples, k=.25, cap=.42, enabled=True):
    """保留区间记录为源采样起止、输出采样起点；crossfade 区有两个来源。"""
    n = len(samples) // HOP
    cuts = []
    threshold = None
    if enabled and n >= 3:
        db = []
        for i in range(n):
            frame = samples[i * HOP:(i + 1) * HOP]
            rms = math.sqrt(sum(v * v for v in frame) / HOP)
            db.append(20 * math.log10(rms + 1e-9))
        # 与原脚本 convolve(..., 'same') 一致：边界用零 dB 填充。
        padded = [0.0] + db + [0.0]
        smoothed = [sum(padded[i:i + 3]) / 3 for i in range(n)]
        threshold = percentile(smoothed, .9) - 35
        start = None
        for i in range(n + 1):
            silent = i < n and smoothed[i] < threshold
            if silent and start is None:
                start = i
            elif not silent and start is not None:
                old = (i - start) / 100
                if start > 0 and i < n and old >= .16:
                    keep = min(.16 + k * (old - .16), cap)
                    a = start * HOP + int(keep * SR / 2)
                    b = i * HOP - int(keep * SR / 2)
                    if b - a > 2 * FADE:
                        cuts.append({"start_sample": a, "end_sample": b,
                                     "old_seconds": old, "keep_formula_seconds": keep,
                                     "crossfade_seconds": .01})
                start = None
    intervals = []
    cursor = 0
    for cut in cuts:
        intervals.append((cursor, cut["start_sample"]))
        cursor = cut["end_sample"]
    intervals.append((cursor, len(samples)))
    output = array("f")
    retained = []
    for a, b in intervals:
        part = samples[a:b]
        overlap = min(FADE, len(output), len(part)) if retained else 0
        out_start = len(output) - overlap
        if overlap:
            for j in range(overlap):
                weight = j / max(1, overlap - 1)
                output[out_start + j] = (output[out_start + j] * (1 - weight)
                                         + part[j] * weight)
        output.extend(part[overlap:])
        retained.append({"source_start_sample": a, "source_end_sample": b,
                         "output_start_sample": out_start})
    return output, retained, {"threshold_db": threshold, "cuts": cuts}


def fade_edges(samples):
    count = min(FADE, len(samples) // 2)
    for i in range(count):
        gain = i / max(1, count - 1)
        samples[i] *= gain
        samples[-i - 1] *= gain


def normalize(src, dst, target):
    first = measure(src, target)
    options = [loud_filter(target)]
    for key, name in (("input_i", "measured_I"), ("input_tp", "measured_TP"),
                      ("input_lra", "measured_LRA"), ("input_thresh", "measured_thresh"),
                      ("target_offset", "offset")):
        options.append("{}={}".format(name, first[key]))
    options.extend(["linear=true", "print_format=json"])
    result = ffmpeg(input_args(src) + ["-map", "0:a:0", "-af", ":".join(options),
                    "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s24le", str(dst)])
    second = loud_json(result.stderr.decode("utf-8", "replace"))
    return {"first_pass": first, "second_pass": second,
            "normalization_type": second.get("normalization_type", "unknown"),
            "filter_second_pass": ":".join(options)}


def optional_processing(src, dst, args, work):
    filters = []
    if args.eq:
        filters.append(EQ)
    if args.deess:
        filters.append(DEESS)
    if args.comp:
        filters.append(COMP)
    room_report = None
    if args.room:
        samples, _ = decode(args.room)
        room_path = work / "room.f32"
        save_raw(room_path, samples)
        result = ffmpeg(input_args(room_path) + ["-af", "astats=metadata=0:reset=0",
                                                "-f", "null", "-"])
        levels = re.findall(r"RMS level dB:\s*([-+\d.eE]+)",
                            result.stderr.decode("utf-8", "replace"))
        if not levels or not math.isfinite(float(levels[-1])):
            raise AudioError("底噪文件无法测量 RMS")
        rms = float(levels[-1])
        gain = -52 - rms
        voice = ",".join(filters) if filters else "anull"
        graph = ("[0:a]{}[voice];[1:a]volume={}dB[bed];"
                 "[voice][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]")
        ffmpeg(input_args(src) + ["-stream_loop", "-1"] + input_args(room_path)
               + ["-filter_complex", graph.format(voice, gain), "-map", "[out]",
                  "-ac", "1", "-ar", str(SR), "-f", "f32le", str(dst)])
        room_report = {"file": str(args.room), "input_rms_dbfs": rms,
                       "gain_db": gain, "bed_rms_dbfs_before_loudnorm": -52,
                       "loop": True}
    elif filters:
        ffmpeg(input_args(src) + ["-af", ",".join(filters), "-ac", "1", "-ar", str(SR),
                                  "-f", "f32le", str(dst)])
    else:
        return src, {"filters": [], "room": None}
    return dst, {"filters": filters, "room": room_report}


def same_file(a, b):
    return a.resolve() == b.resolve() or (a.exists() and b.exists() and os.path.samefile(a, b))


def destinations(out):
    return [out, out.with_suffix(".report.json"), out.with_suffix(".timemap.tsv"),
            out.with_name(out.stem + "-对比原样.wav")]


def validate_outputs(args):
    paths = destinations(args.out)
    if args.out.suffix.lower() != ".wav":
        raise AudioError("--out 必须是 .wav 文件")
    for path in paths:
        for source in args.wav + ([args.room] if args.room else []):
            if same_file(path, source):
                raise AudioError("输出或附带文件与输入是同一个文件，拒绝运行：{}".format(path))
        if path.exists() or path.is_symlink():
            if not path.is_file() or path.is_symlink():
                raise AudioError("输出位置不是普通文件或是软链接：{}".format(path))
            if not args.force:
                raise AudioError("输出已存在，请另取文件名或加 --force：{}".format(path))
    for i, path in enumerate(paths):
        if any(same_file(path, other) for other in paths[:i]):
            raise AudioError("输出文件互相冲突")
    return paths


def publish(staged, paths, force, work):
    """全部成功后发布；无 force 时用独占硬链接，避免并发覆盖。失败回滚。"""
    written = []
    backups = {}
    try:
        for i, (src, dst) in enumerate(zip(staged, paths)):
            if force and dst.exists():
                backup = work / ("backup-" + str(i))
                os.link(dst, backup)
                backups[dst] = backup
            if force:
                os.replace(src, dst)
            else:
                os.link(src, dst)
            written.append(dst)
    except OSError:
        for dst in reversed(written):
            if dst in backups:
                os.replace(backups[dst], dst)
            else:
                dst.unlink()
        raise


def quality_ok(value, target):
    return abs(float(value["input_i"]) - target) <= 1 and float(value["input_tp"]) <= -1.5


def check(args):
    levels = []
    medians = []
    for path in args.wav:
        samples, _ = decode(path)
        duration = len(samples) / SR
        value = measure(path)
        stats = pauses(path, duration)
        levels.append(float(value["input_i"]))
        medians.append(stats["median"])
        print("{}：时长 {:.2f} 秒；响度 I {} LUFS / LRA {} LU / 真峰 {} dBTP；"
              "内部停顿 {} 处 / 中位 {:.3f} 秒 / 最长 {:.3f} 秒".format(
                  path, duration, value["input_i"], value["input_lra"], value["input_tp"],
                  stats["count"], stats["median"], stats["max"]))
    if len(levels) > 1:
        print("各段响度差（最大−最小）：{:.2f} LU".format(max(levels) - min(levels)))
    print("建议：停顿中位最高 {:.2f} 秒；先用 polish 默认参数做同响度对比，再用人耳决定。".format(max(medians)))
    return 0


def polish(args):
    paths = validate_outputs(args)
    if not args.out.parent.is_dir():
        raise AudioError("输出目录不存在：{}".format(args.out.parent))
    with tempfile.TemporaryDirectory(prefix=".tiaoyin-", dir=str(args.out.parent)) as temp:
        work = Path(temp)
        comparison_raw = work / "comparison.f32"
        comparison_samples = array("f")
        segments = []
        parts = []
        mappings = []
        for index, source in enumerate(args.wav):
            original, info = decode(source)
            comparison_samples.extend(original)
            raw_path = work / ("source-{}.f32".format(index))
            save_raw(raw_path, original)
            before_loud = measure(raw_path, args.target)
            before_pauses = pauses(raw_path, len(original) / SR)
            trimmed, start, end = trim(original)
            part, retained, compression = compress_pauses(trimmed, args.k, args.cap,
                                                         not args.keep_pauses)
            part_path = work / ("part-{}.f32".format(index))
            save_raw(part_path, part)
            level = measure(part_path, args.target)
            stats_after = pauses(part_path, len(part) / SR)
            segments.append({"source": str(source), "input_format": info,
                             "duration_before": len(original) / SR,
                             "duration_after_trim": len(trimmed) / SR,
                             "duration_after_compression": len(part) / SR,
                             "trim_source_start": start / SR, "trim_source_end": end / SR,
                             "loudness_before": before_loud, "loudness_for_level_match": level,
                             "level_gain_db": 0.0, "pauses_before": before_pauses,
                             "pauses_after_compression": stats_after, "compression": compression})
            parts.append(part)
            mappings.append((start, retained))
        save_raw(comparison_raw, comparison_samples)
        duration_before = len(comparison_samples) / SR
        del comparison_samples
        before = measure(comparison_raw, args.target)
        pauses_before = pauses(comparison_raw, duration_before)
        median_i = statistics.median(float(s["loudness_for_level_match"]["input_i"])
                                     for s in segments)
        joined = array("f", [0]) * round(.15 * SR)
        map_rows = []
        for index, (part, segment, mapping) in enumerate(zip(parts, segments, mappings)):
            difference = median_i - float(segment["loudness_for_level_match"]["input_i"])
            if len(parts) > 1 and not args.no_level_match and abs(difference) > 1.5:
                gain_db = max(-3.0, min(3.0, difference))
                gain = 10 ** (gain_db / 20)
                for i in range(len(part)):
                    part[i] *= gain
                segment["level_gain_db"] = gain_db
            offset = len(joined)
            trim_start, retained = mapping
            for keep in retained:
                row = [str(args.wav[index]),
                       (trim_start + keep["source_start_sample"]) / SR,
                       (offset + keep["output_start_sample"]) / SR]
                map_rows.append(row)
            segment["retained_ranges"] = retained
            segment["output_start_seconds"] = offset / SR
            fade_edges(part)
            joined.extend(part)
            joined.extend(array("f", [0]) * round((args.gap if index < len(parts) - 1 else .6) * SR))
        joined_raw = work / "joined.f32"
        save_raw(joined_raw, joined)
        edited_duration = len(joined) / SR
        del joined, parts
        premaster, optional = optional_processing(joined_raw, work / "premaster.f32", args, work)
        staged = [work / "final.wav", work / "report.json", work / "timemap.tsv",
                  work / "comparison.wav"]
        normalization = normalize(premaster, staged[0], args.target)
        comparison_normalization = normalize(comparison_raw, staged[3], args.target)
        after = measure(staged[0], args.target)
        comparison_loudness = measure(staged[3], args.target)
        output_info = probe(staged[0])
        duration_after = float(output_info["format"]["duration"])
        pauses_after = pauses(staged[0], duration_after)
        good = quality_ok(after, args.target) and quality_ok(comparison_loudness, args.target)
        version = run(["ffmpeg", "-version"]).stdout.decode("utf-8", "replace").splitlines()[0]
        report = {
            "schema_version": 1, "ffmpeg_version": version,
            "parameters": {"decode": {"sample_rate": SR, "channels": 1, "format": "f32le"},
                           "trim": {"threshold_relative_peak_db": -50, "head_seconds": .03,
                                    "tail_seconds": .12},
                           "compression": {"enabled": not args.keep_pauses, "k": args.k,
                                           "cap": args.cap, "base": .16, "frame": .01,
                                           "smooth_frames": 3, "threshold": "P90(smoothed_db)-35",
                                           "crossfade": .01, "exclude_edges": True},
                           "level_match": {"enabled": len(args.wav) > 1 and not args.no_level_match,
                                           "median_i": median_i, "deadband_lu": 1.5, "max_gain_db": 3},
                           "join": {"head": .15, "gap": args.gap, "tail": .6, "fade": .01},
                           "optional": optional,
                           "loudnorm": {"I": args.target, "TP": -2.0, "LRA": 11,
                                        "passes": 2, "linear_requested": True},
                           "output": {"sample_rate": SR, "channels": 1, "codec": "pcm_s24le"}},
            "segments": segments, "duration_before": duration_before,
            "duration_edited_before_normalization": edited_duration, "duration_after": duration_after,
            "pauses_before": pauses_before, "pauses_after": pauses_after,
            "loudness_before": before, "loudness_after": after, "normalization": normalization,
            "output_format": output_info,
            "comparison": {"file": str(paths[3]), "loudness": comparison_loudness,
                           "normalization": comparison_normalization,
                           "description": "输入解码为48k单声道后原样首尾相接，仅做两遍响度"},
            "quality_passed": good,
            "timemap_note": "保留片段起点；10ms交叉淡化区有两个来源，不代表逐字语义对齐"}
        staged[1].write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                             encoding="utf-8")
        with staged[2].open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            writer.writerow(["源文件", "源秒", "成品秒"])
            for source, src_time, dst_time in map_rows:
                writer.writerow([source, "{:.6f}".format(src_time), "{:.6f}".format(dst_time)])
        # 发布前重查，避免长处理期间出现的新目标或输入/输出别名。
        validate_outputs(args)
        publish(staged, paths, args.force, work)
        enabled = (["EQ清晰"] if args.eq else []) + (["去齿音"] if args.deess else [])
        enabled += (["轻压缩"] if args.comp else []) + (["底噪"] if args.room else [])
        gains = ["第{}段{:+.2f}dB".format(i + 1, s["level_gain_db"])
                 for i, s in enumerate(segments) if s["level_gain_db"]]
        print("时长：{:.2f} 秒 → {:.2f} 秒".format(duration_before, duration_after))
        print("内部停顿：{} → {} 处；中位 {:.3f} → {:.3f} 秒；最长 {:.3f} → {:.3f} 秒".format(
            pauses_before["count"], pauses_after["count"], pauses_before["median"],
            pauses_after["median"], pauses_before["max"], pauses_after["max"]))
        print("响度：{} → {} LUFS；真峰 {} dBTP；两遍模式 {}".format(
            before["input_i"], after["input_i"], after["input_tp"], normalization["normalization_type"]))
        print("处理：{}；{}；可选处理：{}".format(
            "保留停顿" if args.keep_pauses else "按比例压停顿",
            "电平调整 " + "、".join(gains) if gains else "无段级电平调整", "、".join(enabled) or "全关"))
        print("已输出：成品、report.json、timemap.tsv、对比原样；{}，请人耳试听。".format(
            "复测达标" if good else "复测不达标（退出码2）"))
        return 0 if good else 2


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise AudioError("参数错误：" + message)


def main(argv=None):
    parser = Parser(description="口播调音：先 check，再 polish，最后人耳听同响度对比")
    commands = parser.add_subparsers(dest="command", required=True)
    diagnostic = commands.add_parser("check", help="只读诊断")
    diagnostic.add_argument("wav", nargs="+", type=Path)
    processing = commands.add_parser("polish", help="离线压停顿与两遍响度")
    processing.add_argument("wav", nargs="+", type=Path)
    processing.add_argument("--out", required=True, type=Path)
    processing.add_argument("--k", type=float, default=.25)
    processing.add_argument("--cap", type=float, default=.42)
    processing.add_argument("--gap", type=float, default=.5)
    processing.add_argument("--target", type=float, default=-16)
    processing.add_argument("--keep-pauses", action="store_true")
    processing.add_argument("--no-level-match", action="store_true")
    processing.add_argument("--eq", choices=["clear"])
    processing.add_argument("--deess", action="store_true")
    processing.add_argument("--comp", choices=["light"])
    processing.add_argument("--room", type=Path)
    processing.add_argument("--force", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.command == "check":
            return check(args)
        if not (math.isfinite(args.k) and 0 <= args.k <= 1 and
                math.isfinite(args.cap) and args.cap >= .16 and
                math.isfinite(args.gap) and args.gap >= 0 and
                math.isfinite(args.target) and -70 <= args.target <= -5):
            raise AudioError("参数范围：k 为 0–1，cap ≥ 0.16，gap ≥ 0，target 为 -70 至 -5；须为有限数")
        return polish(args)
    except (AudioError, OSError, ValueError) as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
