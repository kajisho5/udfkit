"""Machine-readable contract (`udfkit contract --json`), in the shape the AI Video
Production OS registry reads (skill_id, version, contract_version, provides,
dependencies, not_provided)."""

from . import __version__

SKILL_ID = "udfkit"
CONTRACT_VERSION = "1.0"

TOOLS = [
    {"tool_id": "udfkit/dvd", "kind": "cli", "writes_media": True, "mutates_input": False,
     "description": "Write a DVD-Video image (UDF 1.02 + ISO 9660) of a folder with VIDEO_TS",
     "argv": ["dvd", "<src>", "<image>", "--label", "<NAME>"]},
    {"tool_id": "udfkit/bluray", "kind": "cli", "writes_media": True, "mutates_input": False,
     "description": "Write a Blu-ray image (UDF 2.50) of a folder with BDMV",
     "argv": ["bluray", "<src>", "<image>", "--label", "<NAME>"]},
    {"tool_id": "udfkit/info", "kind": "cli", "writes_media": False, "mutates_input": False,
     "description": "The volume names an image carries, as JSON",
     "argv": ["info", "<image>"]},
    {"tool_id": "udfkit/verify", "kind": "cli", "writes_media": False, "mutates_input": False,
     "description": "Read an image back against the SHA-256 manifest written beside it; "
                    "names each file whose bytes changed (exit 1)",
     "argv": ["verify", "<image>", "--json"]},
]


def contract():
    return {
        "skill_id": SKILL_ID,
        "id": SKILL_ID,
        "version": __version__,
        "contract_version": CONTRACT_VERSION,
        "provides": [
            {"id": "disc_image.dvd", "lifecycle": "EXPERIMENTAL", "tool_id": "udfkit/dvd"},
            {"id": "disc_image.bluray", "lifecycle": "EXPERIMENTAL", "tool_id": "udfkit/bluray"},
            {"id": "disc_image.info", "lifecycle": "EXPERIMENTAL", "tool_id": "udfkit/info"},
            {"id": "disc_image.verify", "lifecycle": "EXPERIMENTAL", "tool_id": "udfkit/verify"},
        ],
        # No Skill dependencies (ffmpeg-skill is not used).
        "dependencies": [],
        "external_tools": [],
        "not_provided": [
            "authoring the VIDEO_TS or BDMV folder (vtskit, bdmvkit, oncemore)",
            "burning a disc",
            "encoding video",
            "network access of any kind",
        ],
        "tools": TOOLS,
        "python_api": ("udfkit.dvd.write_image, udfkit.bluray.write_image, udfkit.info.volume_names, "
                       "udfkit.manifest.write_manifest, udfkit.manifest.verify"),
        "execution": {"canonical_invocation": ["udfkit", "contract", "--json"],
                      "network": False, "shell": False},
    }
