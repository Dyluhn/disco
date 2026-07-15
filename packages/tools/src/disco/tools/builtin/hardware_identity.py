"""Structured, provenance-bearing hardware identity lookup.

Numeric bus identifiers are evidence, but they are not marketing names.  This
tool resolves PCI vendor/device pairs against the sandbox's local ``pci.ids``
database and fails closed to the numeric identifiers when no exact mapping is
available.  It never contains a product-specific fallback table.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from disco.core import SecurityRisk
from pydantic import BaseModel, Field, field_validator

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

_PCI_ID = re.compile(r"(?:0x)?([0-9a-fA-F]{4})\Z")
_PCI_DATABASE_PATHS = (
    "/usr/share/hwdata/pci.ids",
    "/usr/share/misc/pci.ids",
    "/usr/share/pci.ids",
)


class HardwareIdentityArgs(BaseModel):
    bus: Literal["pci"] = Field(description="Hardware bus; currently PCI only.")
    vendor_id: str = Field(description="Exact four-digit PCI vendor ID, with optional 0x.")
    device_id: str = Field(description="Exact four-digit PCI device ID, with optional 0x.")

    @field_validator("vendor_id", "device_id")
    @classmethod
    def _normalize_pci_id(cls, value: str) -> str:
        match = _PCI_ID.fullmatch(value.strip())
        if match is None:
            raise ValueError("PCI IDs must be exactly four hexadecimal digits")
        return f"0x{match.group(1).lower()}"


def _lookup_command(vendor_id: str, device_id: str) -> str:
    """Build a fixed-shape probe; normalized hexadecimal IDs cannot inject shell."""
    vendor = vendor_id.removeprefix("0x")
    device = device_id.removeprefix("0x")
    paths = " ".join(_PCI_DATABASE_PATHS)
    return f"""vendor={vendor}; device={device}
for path in {paths}; do
  if [ -r "$path" ]; then
    printf 'SOURCE\\t%s\\n' "$path"
    awk -v vendor="$vendor" -v device="$device" '
      /^[[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]]  / {{
        if (in_vendor) exit
        in_vendor = (tolower(substr($0, 1, 4)) == vendor)
        if (in_vendor) print "VENDOR\\t" substr($0, 7)
        next
      }}
      in_vendor && /^\\t[[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]]  / &&
        tolower(substr($0, 2, 4)) == device {{
          print "DEVICE\\t" substr($0, 8)
          exit
        }}
    ' "$path"
    exit 0
  fi
done
exit 0"""


def _parse_lookup(
    output: str,
    *,
    vendor_id: str,
    device_id: str,
) -> dict[str, object]:
    source: str | None = None
    vendor_name: str | None = None
    device_name: str | None = None
    allowed_sources = frozenset(_PCI_DATABASE_PATHS)

    for line in output.splitlines():
        kind, separator, value = line.partition("\t")
        if not separator:
            continue
        value = value.strip()
        if kind == "SOURCE" and value in allowed_sources:
            source = value
        elif kind == "VENDOR" and value:
            vendor_name = value
        elif kind == "DEVICE" and value:
            device_name = value

    return {
        "bus": "pci",
        "vendor_id": vendor_id,
        "device_id": device_id,
        "status": "recognized" if device_name is not None else "unknown",
        "vendor_name": vendor_name,
        "device_name": device_name,
        "mapping_source": ({"kind": "pci.ids", "path": source} if source is not None else None),
    }


class HardwareIdentityTool:
    definition = ToolDef(
        name="hardware_identity",
        description=(
            "Resolve an exact numeric PCI vendor/device pair against the sandbox's "
            "local pci.ids database. Returns structured recognized/unknown evidence, "
            "the unchanged numeric IDs, and mapping provenance. Use this before "
            "stating a hardware marketing or product name; never guess a name when "
            "status is unknown."
        ),
        args_model=HardwareIdentityArgs,
        needs=frozenset({Capability.SHELL}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=True,
    )

    async def run(self, args: HardwareIdentityArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        timeout_s = min(10, max(1, ctx.timeout_s))
        result = await ctx.sandbox.exec_shell(
            _lookup_command(args.vendor_id, args.device_id),
            timeout_s=timeout_s,
        )
        if result.exit_code != 0 or result.timed_out:
            detail = (result.stderr or result.stdout).strip()
            reason = "timed out" if result.timed_out else f"exited {result.exit_code}"
            if detail:
                reason = f"{reason}: {detail}"
            return ToolOutcome(
                success=False,
                content="PCI identity lookup failed; no identity claim is available.",
                structured={
                    "bus": "pci",
                    "vendor_id": args.vendor_id,
                    "device_id": args.device_id,
                    "status": "lookup_error",
                },
                error=f"PCI identity lookup {reason}",
            )

        evidence = _parse_lookup(
            result.stdout,
            vendor_id=args.vendor_id,
            device_id=args.device_id,
        )
        return ToolOutcome(
            success=True,
            content=json.dumps(evidence, sort_keys=True),
            structured=evidence,
        )
