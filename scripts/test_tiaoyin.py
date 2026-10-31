"""离线回归：音频全部由本机 ffmpeg lavfi 生成，不读真实片子。"""
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).with_name("tiaoyin.py")
spec = importlib.util.spec_from_file_location("tiaoyin", SCRIPT)
tiaoyin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tiaoyin)
PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable


@pytest.fixture(autouse=True)
def clean_test_audio(tmp_path):
    # pytest 默认会保留历史 tmp_path；本套测试不保留合成音频。
    yield
    shutil.rmtree(str(tmp_path), ignore_errors=True)


def tool(argv):
    result = subprocess.run(argv, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result


def synth(path, pause=.8, gain=0, stereo=False, tone=1.5):
    # 首尾静音用来检验留边；中间停顿为已知值。
    graph = ("anullsrc=r=48000:cl=mono:d=0.25[a];"
             "sine=f=440:r=48000:d={tone}[b];"
             "anullsrc=r=48000:cl=mono:d={pause}[c];"
             "sine=f=660:r=48000:d={tone}[d];"
             "anullsrc=r=48000:cl=mono:d=0.35[e];"
             "[a][b][c][d][e]concat=n=5:v=0:a=1,volume={gain}dB[out]").format(
                 tone=tone, pause=pause, gain=gain)
    tool(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
          "-i", graph, "-ar", "48000", "-ac", "2" if stereo else "1", "-c:a", "pcm_s24le", str(path)])
    return path


def cli(*args):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([PYTHON, str(SCRIPT)] + [str(a) for a in args],
                          capture_output=True, text=True, timeout=120, env=env)


def polish(inputs, out, *options):
    result = cli("polish", *inputs, "--out", out, *options)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(result.stdout.splitlines()) <= 6
    return json.loads(out.with_suffix(".report.json").read_text()), result


def assert_loud(path, target=-16):
    measured = tiaoyin.measure(path, target)
    assert abs(float(measured["input_i"]) - target) <= .5
    assert float(measured["input_tp"]) <= -1.5


@pytest.mark.parametrize("old,expected", [(.20, .17), (.40, .22), (.80, .32), (1.20, .42)])
def test_pause_math(tmp_path, old, expected):
    src = synth(tmp_path / "source.wav", pause=old, tone=.5)
    samples, _ = tiaoyin.decode(src)
    trimmed, _, _ = tiaoyin.trim(samples)
    output, retained, audit = tiaoyin.compress_pauses(trimmed)
    raw = tmp_path / "edited.f32"
    tiaoyin.save_raw(raw, output)
    # 短停顿经原算法的窗边界和 crossfade 后可能略小于 150ms。
    # 数学测试独立用 50ms 检测；产品统计仍保持 d=0.15。
    result = tiaoyin.ffmpeg(tiaoyin.input_args(raw) + [
        "-af", "silencedetect=noise=-40dB:d=0.05", "-f", "null", "-"])
    ends = re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)",
                      result.stderr.decode())
    lengths = [float(length) for end, length in ends
               if float(end) < len(output) / tiaoyin.SR - .001 and float(end) - float(length) > .001]
    assert len(lengths) == 1
    assert lengths[0] == pytest.approx(expected, abs=.03)
    assert len(audit["cuts"]) == 1
    assert len(retained) == 2


def test_keep_pauses(tmp_path):
    src = synth(tmp_path / "src.wav", pause=.8)
    report, _ = polish([src], tmp_path / "out.wav", "--keep-pauses")
    segment = report["segments"][0]
    assert segment["compression"]["cuts"] == []
    assert segment["duration_after_compression"] == segment["duration_after_trim"]
    assert report["pauses_after"]["median"] == pytest.approx(.8, abs=.03)


def test_gap_not_compressed(tmp_path):
    a = synth(tmp_path / "a.wav", pause=1.2)
    b = synth(tmp_path / "b.wav", pause=.8)
    report, _ = polish([a, b], tmp_path / "out.wav", "--gap", ".7")
    lengths = [end - start for start, end in report["pauses_after"]["intervals"]]
    long_ = [length for length in lengths if length > .5]
    assert long_ == pytest.approx([.7 + .03 + .12], abs=.05)
    assert len(lengths) == 3
    assert max(length for length in lengths if length < .5) <= .45


def test_level_match_bounded(tmp_path):
    inputs = [synth(tmp_path / (str(i) + ".wav"), gain=gain)
              for i, gain in enumerate([0, 0, -6])]
    report, _ = polish(inputs, tmp_path / "out.wav")
    adjustments = [s["level_gain_db"] for s in report["segments"]]
    assert adjustments == pytest.approx([0, 0, 3], abs=.01)


def test_no_level_match(tmp_path):
    inputs = [synth(tmp_path / (str(i) + ".wav"), gain=gain)
              for i, gain in enumerate([0, 0, -6])]
    report, _ = polish(inputs, tmp_path / "out.wav", "--no-level-match")
    assert all(s["level_gain_db"] == 0 for s in report["segments"])


def test_final_quality_format_and_unchanged_inputs(tmp_path):
    src = synth(tmp_path / "src.wav")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path / "out.wav"
    report, _ = polish([src], out)
    assert_loud(out)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    info = tiaoyin.probe(out)["streams"][0]
    assert info == {"codec_name": "pcm_s24le", "sample_rate": "48000", "channels": 1}
    assert report["normalization"]["normalization_type"] in ("linear", "dynamic")
    assert report["parameters"]["loudnorm"]["TP"] == -2
    assert report["ffmpeg_version"].startswith("ffmpeg version")
    assert report["quality_passed"]
    assert not list(tmp_path.glob(".tiaoyin-*"))


@pytest.mark.parametrize("alias", ["same", "hardlink", "symlink"])
def test_refuse_output_alias(tmp_path, alias):
    src = synth(tmp_path / "src.wav")
    before = src.read_bytes()
    out = src if alias == "same" else tmp_path / "alias.wav"
    if alias == "hardlink":
        os.link(src, out)
    elif alias == "symlink":
        out.symlink_to(src)
    result = cli("polish", src, "--out", out, "--force")
    assert result.returncode == 3
    assert "错误" in result.stderr
    assert src.read_bytes() == before
    assert not src.with_suffix(".report.json").exists()


def test_existing_output_refused(tmp_path):
    src = synth(tmp_path / "src.wav")
    out = tmp_path / "out.wav"
    out.write_bytes(b"do not overwrite")
    result = cli("polish", src, "--out", out)
    assert result.returncode == 3
    assert "已存在" in result.stderr
    assert out.read_bytes() == b"do not overwrite"
    assert not out.with_suffix(".report.json").exists()


def test_existing_companion_refused(tmp_path):
    src = synth(tmp_path / "src.wav")
    out = tmp_path / "out.wav"
    companion = out.with_suffix(".timemap.tsv")
    companion.write_text("keep me")
    assert cli("polish", src, "--out", out).returncode == 3
    assert not out.exists()
    assert companion.read_text() == "keep me"


def test_force(tmp_path):
    src = synth(tmp_path / "src.wav")
    out = tmp_path / "out.wav"
    out.write_bytes(b"old")
    polish([src], out, "--force")
    assert_loud(out)


@pytest.mark.parametrize("kind", ["silence", "empty", "text", "missing"])
def test_bad_input_no_partial(tmp_path, kind):
    src = tmp_path / "bad.wav"
    if kind == "silence":
        tool(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
              "anullsrc=r=48000:cl=mono:d=2", str(src)])
    elif kind == "empty":
        src.touch()
    elif kind == "text":
        src.write_text("not audio")
    out = tmp_path / "out.wav"
    result = cli("polish", src, "--out", out)
    assert result.returncode == 3
    assert "错误" in result.stderr
    assert not list(tmp_path.glob("out*"))
    assert not list(tmp_path.glob(".tiaoyin-*"))


def test_later_invalid_input_no_partial(tmp_path):
    src = synth(tmp_path / "src.wav")
    bad = tmp_path / "bad.wav"
    bad.write_text("not audio")
    assert cli("polish", src, bad, "--out", tmp_path / "out.wav").returncode == 3
    assert not list(tmp_path.glob("out*"))
    assert not list(tmp_path.glob(".tiaoyin-*"))


def test_stereo_to_mono(tmp_path):
    src = synth(tmp_path / "stereo.wav", stereo=True)
    out = tmp_path / "out.wav"
    polish([src], out)
    assert tiaoyin.probe(src)["streams"][0]["channels"] == 2
    assert tiaoyin.probe(out)["streams"][0]["channels"] == 1
    assert_loud(out)


@pytest.mark.parametrize("options", [["--eq", "clear"], ["--deess"], ["--comp", "light"],
                                    ["--room"], ["--eq", "clear", "--deess", "--comp", "light", "--room"]])
def test_optional_effects(tmp_path, options):
    src = synth(tmp_path / "src.wav")
    if "--room" in options:
        room = tmp_path / "room.wav"
        tool(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
              "anoisesrc=color=pink:sample_rate=48000:duration=0.7:seed=17", str(room)])
        options = options + [str(room)]
    out = tmp_path / "out.wav"
    report, _ = polish([src], out, *options)
    assert_loud(out)
    enabled = report["parameters"]["optional"]
    assert enabled["filters"] or enabled["room"]
    if "--room" in options:
        bed = enabled["room"]
        assert bed["input_rms_dbfs"] + bed["gain_db"] == pytest.approx(-52)


def test_timemap_and_comparison(tmp_path):
    inputs = [synth(tmp_path / (str(i) + ".wav"), pause=pause)
              for i, pause in enumerate([.8, 1.2])]
    out = tmp_path / "out.wav"
    report, _ = polish(inputs, out)
    with out.with_suffix(".timemap.tsv").open() as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        assert reader.fieldnames == ["源文件", "源秒", "成品秒"]
        rows = list(reader)
    times = [float(row["成品秒"]) for row in rows]
    assert len(times) == 4
    assert all(a < b for a, b in zip(times, times[1:]))
    assert times[-1] < report["duration_after"]
    assert float(rows[0]["源秒"]) == pytest.approx(.22, abs=.001)
    assert times[0] == pytest.approx(.15)
    # 删去样本和 crossfade 重叠长度必须反映到映射起点。
    for index, segment in enumerate(report["segments"]):
        cut = segment["compression"]["cuts"][0]
        kept = segment["retained_ranges"][1]
        assert kept["source_start_sample"] == cut["end_sample"]
        expected = segment["output_start_seconds"] + (cut["start_sample"] - 480) / 48000
        assert times[2 * index + 1] == pytest.approx(expected, abs=.000002)
    comparison = out.with_name("out-对比原样.wav")
    assert comparison.is_file()
    assert_loud(comparison)
    duration = float(tiaoyin.probe(comparison)["format"]["duration"])
    assert duration == pytest.approx(report["duration_before"], abs=.001)


def test_check_readonly(tmp_path):
    a = synth(tmp_path / "a.wav")
    b = synth(tmp_path / "b.wav", gain=-6)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    result = cli("check", a, b)
    assert result.returncode == 0, result.stderr
    assert all(word in result.stdout for word in ("响度 I", "LRA", "真峰", "停顿", "中位", "最长", "建议", "各段响度差"))
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    assert before == after


def test_boundary_silence_excluded(tmp_path):
    src = synth(tmp_path / "src.wav", pause=.8)
    duration = float(tiaoyin.probe(src)["format"]["duration"])
    value = tiaoyin.pauses(src, duration)
    assert value["count"] == 1
    assert value["median"] == pytest.approx(.8, abs=.01)


def test_ffmpeg_failure_cleans_stage(tmp_path, monkeypatch, capsys):
    src = synth(tmp_path / "src.wav")
    def fail(*args, **kwargs):
        raise tiaoyin.AudioError("ffmpeg 执行失败（模拟）")
    monkeypatch.setattr(tiaoyin, "measure", fail)
    code = tiaoyin.main(["polish", str(src), "--out", str(tmp_path / "out.wav")])
    assert code == 3
    assert "错误" in capsys.readouterr().err
    assert not list(tmp_path.glob("out*"))
    assert not list(tmp_path.glob(".tiaoyin-*"))


def test_quality_failure_exit_two(tmp_path, monkeypatch):
    src = synth(tmp_path / "src.wav")
    monkeypatch.setattr(tiaoyin, "quality_ok", lambda *args: False)
    out = tmp_path / "out.wav"
    assert tiaoyin.main(["polish", str(src), "--out", str(out)]) == 2
    report = json.loads(out.with_suffix(".report.json").read_text())
    assert report["quality_passed"] is False


@pytest.mark.parametrize("options", [["--k", "nan"], ["--gap", "-1"], ["--cap", ".1"], ["--target", "-1"]])
def test_invalid_parameters(tmp_path, options):
    result = cli("polish", tmp_path / "src.wav", "--out", tmp_path / "out.wav", *options)
    assert result.returncode == 3
    assert "参数范围" in result.stderr
    assert not list(tmp_path.glob("out*"))


def test_publish_rollback(tmp_path, monkeypatch):
    work = tmp_path / "stage"
    work.mkdir()
    outputs = [tmp_path / (str(i) + ".wav") for i in range(4)]
    stages = [work / (str(i) + ".wav") for i in range(4)]
    for out, stage in zip(outputs, stages):
        out.write_bytes(b"original")
        stage.write_bytes(b"new")
    replace = os.replace
    def fail_third(src, dst):
        if Path(src) == stages[2]:
            raise OSError("模拟发布失败")
        return replace(src, dst)
    monkeypatch.setattr(tiaoyin.os, "replace", fail_third)
    with pytest.raises(OSError):
        tiaoyin.publish(stages, outputs, True, work)
    assert all(out.read_bytes() == b"original" for out in outputs)
