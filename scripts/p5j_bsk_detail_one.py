"""
P5-J: single classified candidate detail probe via BrowserSkill (desktop).

Does not re-search. Does not run 10-sample or full batch. Explicit --backend bsk only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = (
    PROJECT_ROOT
    / "artifacts"
    / "classified_detail_samples"
    / "20261027T064059Z_p5i_9d60d3"
    / "sample_manifest.json"
)
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "config"
    / "policies"
    / "b913a_model_scope_include_other_20261027.json"
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="P5-J BSK single detail probe")
    p.add_argument("--from-manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--product-id", default="1556260334")
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    p.add_argument(
        "--business-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "products" / "b913a.json",
    )
    p.add_argument("--bsk-browser", default="2636aa80", help="BrowserSkill instance id")
    p.add_argument("--offline-only", action="store_true")
    args = p.parse_args(argv)

    # Reuse P5-I orchestration with hard filters.
    sys.path.insert(0, str(SCRIPTS_DIR))
    import p5i_classified_detail_sample as p5i  # noqa: E402

    return p5i.main(
        [
            "--from-manifest",
            str(args.from_manifest),
            "--product-id",
            str(args.product_id),
            "--policy",
            str(args.policy),
            "--business-config",
            str(args.business_config),
            "--backend",
            "bsk",
            "--max-total",
            "1",
            "--max-target",
            "1",
            "--max-other",
            "0",
            *(["--bsk-browser", args.bsk_browser] if args.bsk_browser else []),
            *(["--offline-only"] if args.offline_only else ["--live"]),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
