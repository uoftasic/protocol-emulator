from pathlib import Path
import os
import shlex
import subprocess
import sys
import tempfile


def main():
    test_dir = Path(__file__).resolve().parent
    core_dir = (
        test_dir.parents[2]
        / "src/L1-PEm-system/L2-PEm-engine/L3-PEm-core-array/L4-PEm-core"
    )
    core_file = core_dir / "pem_core.sv"
    divider_file = core_dir / "clock_divider.sv"

    if not core_file.is_file() or not core_file.read_text().strip():
        print(f"Cannot lint: {core_file} is missing or empty.", file=sys.stderr)
        return 2

    verilator = shlex.split(os.environ.get("VERILATOR", "verilator"))
    if not verilator:
        print("Cannot lint: VERILATOR is empty.", file=sys.stderr)
        return 2

    build_dir = test_dir / "sim_build"
    build_dir.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lint-", dir=build_dir) as work_dir:
        work_dir = Path(work_dir)
        if not divider_file.is_file() or not divider_file.read_text().strip():
            divider_file = work_dir / "clock_divider.sv"
            divider_file.write_text(
                "module clock_divider (\n"
                "    input  logic        clk,\n"
                "    input  logic        rst,\n"
                "    input  logic        i_enable,\n"
                "    input  logic [15:0] divisor,\n"
                "    output logic        clk_en\n"
                ");\n"
                "    assign clk_en = 1'b0;\n"
                "endmodule\n\n"
            )
            print("Lint uses a temporary divider interface stub.", flush=True)

        command = verilator + [
            "--lint-only",
            "--Wall",
            "-Wno-fatal",
            "--top-module", "pem_core",
            "--Mdir", str(work_dir / "obj_dir"),
            f"-I{core_dir}",
            str(core_file),
            str(divider_file),
        ]
        try:
            return subprocess.run(command, cwd=test_dir, check=False).returncode
        except FileNotFoundError:
            print(f"Cannot lint: executable {verilator[0]!r} was not found.", file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())

