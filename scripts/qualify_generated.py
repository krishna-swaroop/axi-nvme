"""Exercise generated stream-DMA Verilog in Icarus, independent of Migen's simulator."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

from cocotb_tools.runner import get_runner
from migen.fhdl import verilog
from litenvme.streamer import LiteNVMeStreamDMA

ROOT = Path(__file__).resolve().parents[1]


def export_dma(path, width):
    dma = LiteNVMeStreamDMA(data_width=width, max_reads=4)
    ports = []
    for name in ("start", "we", "adr", "beats", "last_en", "busy"):
        signal = getattr(dma, name)
        signal.name_override = name
        ports.append(signal)
    for prefix, record, names in (
        ("s_", dma.sink, ("valid", "ready", "data")),
        ("m_", dma.source, ("valid", "ready", "data", "last")),
    ):
        for name in names:
            signal = getattr(record, name)
            signal.name_override = prefix + name
            ports.append(signal)
    channels = {
        "aw": ("valid", "ready", "addr", "len", "size", "burst", "id"),
        "w": ("valid", "ready", "data", "strb", "last"),
        "b": ("valid", "ready", "resp", "id"),
        "ar": ("valid", "ready", "addr", "len", "size", "burst", "id"),
        "r": ("valid", "ready", "data", "resp", "last", "id"),
    }
    for channel, names in channels.items():
        # AXI record's own names can be ambiguous across channels; use a stable
        # channel-qualified name for every exported bitvector.
        record = getattr(dma.axi, channel)
        for name in names:
            signal = getattr(record, name)
            signal.name_override = "axi_" + channel + "_" + name
            ports.append(signal)
    converted = verilog.convert(dma, ios=set(ports), name="dma_under_test")
    path.write_text(converted.main_source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junitxml", type=Path, default=ROOT / "reports/development/generated.xml")
    args = parser.parse_args()
    args.junitxml = args.junitxml.resolve()
    for executable in ("iverilog", "vvp"):
        if not shutil.which(executable):
            raise SystemExit(f"Missing required simulator: {executable}")
    args.junitxml.parent.mkdir(parents=True, exist_ok=True)
    args.junitxml.unlink(missing_ok=True)
    suite = ET.Element("testsuites")
    metadata = {"litenvme_module": str(Path(sys.modules["litenvme.streamer"].__file__).resolve()),
                "configurations": []}
    for width in (64, 128, 256, 512):
        build = ROOT / f"build/qualification/dma-{width}"
        build.mkdir(parents=True, exist_ok=True)
        source = build / "dma.v"
        export_dma(source, width)
        runner = get_runner("icarus")
        runner.build(sources=[source], hdl_toplevel="dma_under_test", build_dir=build,
                     always=True, waves=True, timescale=("1ns", "1ps"))
        result = args.junitxml.parent / f"generated-{width}.xml"
        result.unlink(missing_ok=True)
        runner.test(hdl_toplevel="dma_under_test", test_module="dma_cocotb",
                    test_dir=ROOT / "tests/qualification", results_xml=str(result),
                    waves=True, seed=20261021 + width)
        tree = ET.parse(result)
        cases = list(tree.iter("testcase"))
        if not cases or any(c.find("failure") is not None or c.find("error") is not None
                            or c.find("skipped") is not None for c in cases):
            raise SystemExit(f"Generated {width}-bit DMA failed qualification: {result}")
        for child in tree.getroot():
            suite.append(child)
        metadata["configurations"].append({"width": width, "tests": len(cases),
                                           "cocotb_seed": 20261021 + width,
                                           "stimulus_seed": 0x4E564D45 + width,
                                           "result": str(result)})
    ET.ElementTree(suite).write(args.junitxml, encoding="unicode")
    (args.junitxml.parent / "generated-manifest.json").write_text(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
