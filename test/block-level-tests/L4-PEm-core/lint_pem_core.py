import os
from pathlib import Path
import subprocess
import tempfile


def main():
    test_dir = Path(__file__).resolve().parent
    core_dir = (
        test_dir.parents[2]
        / "src/L1-PEm-system/L2-PEm-engine/L3-PEm-core-array/L4-PEm-core"
    )
    build_dir = test_dir / "sim_build"
    build_dir.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lint_", dir=build_dir) as temporary_dir:
        divider_path = core_dir / "clock_divider.sv"
        if not divider_path.read_text().strip():
            divider_path = Path(temporary_dir) / "clock_divider.sv"
            divider_path.write_text(
                "module clock_divider (\n"
                "    input logic clk,\n"
                "    input logic rst,\n"
                "    input logic [15:0] divisor,\n"
                "    output logic clk_en\n"
                ");\n"
                "    assign clk_en = 1'b0;\n"
                "endmodule\n\n"
            )
            print("Using a temporary clock_divider interface stub.", flush=True)

        command = [
            os.environ.get("VERILATOR", "verilator"),
            "--lint-only",
            "--Wall",
            "-Wno-fatal",
            "--top-module",
            "pem_core",
            str(core_dir / "pem_core.sv"),
            str(divider_path),
        ]
        return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

