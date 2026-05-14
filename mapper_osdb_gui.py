#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests>=2.32,<3",
# ]
# ///
"""
Mapper OSDB Builder

A small Tkinter GUI that:
1. Uses osu!api v2 to fetch beatmapsets for a mapper.
2. Extracts beatmap IDs from selected categories.
3. Writes a beatmap_ids.txt file.
4. Optionally calls Collection Manager CLI to create a .osdb file.

Recommended contained run:
    uv run mapper_osdb_gui.py

Optional reproducible lockfile:
    uv lock --script mapper_osdb_gui.py
    uv run --script mapper_osdb_gui.py

Fallback without uv:
    pip install requests
    python mapper_osdb_gui.py
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import queue
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "Mapper OSDB Builder"
CONFIG_PATH = Path.home() / ".mapper_osdb_builder.json"
API_BASE = "https://osu.ppy.sh/api/v2"
TOKEN_URL = "https://osu.ppy.sh/oauth/token"

BEATMAPSET_TYPES = ["ranked", "loved", "pending", "graveyard", "guest"]
RULESETS = {
    "All modes": None,
    "osu!": "osu",
    "taiko": "taiko",
    "catch": "fruits",
    "mania": "mania",
}

PLAY_MODE_BYTES = {
    "osu": 0,
    "taiko": 1,
    "fruits": 2,
    "catch": 2,
    "mania": 3,
}


@dataclass
class FetchOptions:
    client_id: str
    client_secret: str
    mapper: str
    selected_types: list[str]
    ruleset: str | None
    include_all_diffs_in_hosted_sets: bool
    include_guest_sets: bool
    star_min: float
    star_max: float
    ar_min: float
    ar_max: float
    output_folder: Path
    collection_name: str
    cm_cli_path: Path | None
    osu_location: Path | None
    generate_osdb: bool


@dataclass
class BeatmapRecord:
    map_id: int
    mapset_id: int
    artist: str
    title: str
    difficulty: str
    md5: str
    play_mode: int
    stars: float
    ar: float
    url: str
    beatmapset_url: str


class OsuApi:
    def __init__(self, client_id: str, client_secret: str, log):
        self.client_id = client_id
        self.client_secret = client_secret
        self.log = log
        self.session = requests.Session()

    def authenticate(self) -> None:
        self.log("Requesting osu!api token...")
        response = requests.post(
            TOKEN_URL,
            json={
                "client_id": int(self.client_id),
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
                "scope": "public",
            },
            timeout=30,
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.log("Authenticated.")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(6):
            response = self.session.get(f"{API_BASE}{path}", params=params, timeout=30)

            if response.status_code == 429:
                wait = 2 + attempt * 2
                self.log(f"Rate limited; waiting {wait}s...")
                time.sleep(wait)
                continue

            response.raise_for_status()
            return response.json()

        raise RuntimeError(f"Failed after retries: {path}")

    def get_user(self, mapper: str) -> dict[str, Any]:
        mapper = mapper.strip()
        if mapper.isdigit():
            user_key = mapper
        else:
            user_key = quote("@" + mapper, safe="")

        return self.get(f"/users/{user_key}/osu")

    def fetch_user_beatmapsets(self, user_id: int, beatmapset_type: str):
        offset = 0
        limit = 50

        while True:
            data = self.get(
                f"/users/{user_id}/beatmapsets/{beatmapset_type}",
                params={"limit": limit, "offset": offset},
            )

            if not data:
                return

            for item in data:
                yield item

            if len(data) < limit:
                return

            offset += limit
            time.sleep(1.05)

    def get_beatmapset(self, set_id: int) -> dict[str, Any]:
        return self.get(f"/beatmapsets/{set_id}")


def safe_collection_filename(name: str) -> str:
    invalid = '<>:"/\\|?*'
    clean = "".join("_" if ch in invalid else ch for ch in name).strip()
    return clean or "mapper_collection"


def beatmap_ruleset(beatmap: dict[str, Any]) -> str | None:
    # osu!api responses may expose mode differently depending on shape.
    return beatmap.get("mode") or beatmap.get("ruleset")


def is_owned_by_user(
    beatmap: dict[str, Any],
    user_id: int,
    beatmapset_owner_id: int | None = None,
) -> bool:
    """Return True when the mapper appears to own this difficulty.

    osu!api responses vary a bit depending on endpoint/shape, so this checks:
    1. beatmap.user_id when present,
    2. beatmap.owners when present,
    3. beatmapset owner as a safe fallback for solo/hosted sets with no owners list.
    """
    if beatmap.get("user_id") == user_id:
        return True

    owners = beatmap.get("owners") or []
    if any(owner.get("id") == user_id for owner in owners):
        return True

    if beatmapset_owner_id == user_id and not owners and beatmap.get("user_id") is None:
        return True

    return False


def placeholder_md5_for_map_id(map_id: int) -> str:
    """Create a stable placeholder hash when osu!api does not expose a checksum.

    Collection Manager uses hashes internally, but downloadable .osdb entries can still carry
    map ID, mapset ID, title, artist, difficulty, mode, and stars. A stable unique placeholder
    prevents all API-only maps from collapsing into one blank-hash entry.
    """
    return hashlib.md5(f"mapper-osdb-builder:{map_id}".encode("utf-8")).hexdigest()


def beatmap_stars(beatmap: dict[str, Any]) -> float:
    return float(beatmap.get("difficulty_rating") or beatmap.get("stars") or 0.0)


def beatmap_ar(beatmap: dict[str, Any]) -> float:
    value = beatmap.get("ar")
    return float(value) if value is not None else 0.0


def make_beatmap_record(beatmapset: dict[str, Any], beatmap: dict[str, Any]) -> BeatmapRecord:
    map_id = int(beatmap["id"])
    mapset_id = int(beatmap.get("beatmapset_id") or beatmapset["id"])
    mode = beatmap_ruleset(beatmap) or "osu"

    checksum = beatmap.get("checksum") or beatmap.get("md5") or ""
    md5 = checksum if isinstance(checksum, str) and checksum.strip() else placeholder_md5_for_map_id(map_id)

    return BeatmapRecord(
        map_id=map_id,
        mapset_id=mapset_id,
        artist=str(beatmapset.get("artist") or beatmapset.get("artist_unicode") or ""),
        title=str(beatmapset.get("title") or beatmapset.get("title_unicode") or ""),
        difficulty=str(beatmap.get("version") or ""),
        md5=md5,
        play_mode=PLAY_MODE_BYTES.get(str(mode), 0),
        stars=beatmap_stars(beatmap),
        ar=beatmap_ar(beatmap),
        url=f"https://osu.ppy.sh/beatmaps/{map_id}",
        beatmapset_url=f"https://osu.ppy.sh/beatmapsets/{mapset_id}",
    )


def collect_beatmap_ids(options: FetchOptions, log) -> tuple[dict[str, Any], dict[int, BeatmapRecord], dict[str, int]]:
    api = OsuApi(options.client_id, options.client_secret, log)
    api.authenticate()

    user = api.get_user(options.mapper)
    user_id = int(user["id"])
    username = user["username"]
    log(f"Resolved mapper: {username} ({user_id})")

    selected_types = list(options.selected_types)
    if not options.include_guest_sets and "guest" in selected_types:
        selected_types.remove("guest")

    beatmap_records: dict[int, BeatmapRecord] = {}
    stats = {key: 0 for key in selected_types}
    seen_set_ids: set[tuple[str, int]] = set()

    for bm_type in selected_types:
        log(f"Fetching {bm_type} beatmapsets...")

        for beatmapset in api.fetch_user_beatmapsets(user_id, bm_type):
            set_id = int(beatmapset["id"])
            set_owner_id = beatmapset.get("user_id")
            if set_owner_id is not None:
                set_owner_id = int(set_owner_id)

            dedupe_key = (bm_type, set_id)
            if dedupe_key in seen_set_ids:
                continue
            seen_set_ids.add(dedupe_key)

            beatmaps = beatmapset.get("beatmaps") or []
            if (
                not beatmaps
                or not beatmapset.get("artist")
                or not beatmapset.get("title")
                or any("ar" not in b or "difficulty_rating" not in b for b in beatmaps)
            ):
                full = api.get_beatmapset(set_id)
                beatmapset = {**beatmapset, **full}
                beatmaps = full.get("beatmaps") or beatmaps
                time.sleep(1.05)

            before_count = len(beatmap_records)

            for beatmap in beatmaps:
                if options.ruleset and beatmap_ruleset(beatmap) != options.ruleset:
                    continue

                if bm_type == "guest":
                    # Guest category already means the user contributed, but this check
                    # helps narrow to the user's own GDs when owners are available.
                    if not is_owned_by_user(beatmap, user_id):
                        continue
                elif not options.include_all_diffs_in_hosted_sets:
                    # Hosted mapset, but only include diffs owned by this user.
                    if not is_owned_by_user(beatmap, user_id, set_owner_id):
                        continue

                stars = beatmap_stars(beatmap)
                ar = beatmap_ar(beatmap)

                if not (options.star_min <= stars <= options.star_max):
                    continue
                if not (options.ar_min <= ar <= options.ar_max):
                    continue

                beatmap_id = beatmap.get("id")
                if beatmap_id:
                    record = make_beatmap_record(beatmapset, beatmap)
                    beatmap_records[record.map_id] = record

            stats[bm_type] += len(beatmap_records) - before_count

    log(f"Collected {len(beatmap_records)} unique beatmaps.")
    return user, beatmap_records, stats


def write_ids_file(output_folder: Path, collection_name: str, beatmap_ids: set[int]) -> Path:
    output_folder.mkdir(parents=True, exist_ok=True)
    ids_path = output_folder / f"{safe_collection_filename(collection_name)}.beatmap_ids.txt"
    ids_path.write_text(chr(10).join(str(x) for x in sorted(beatmap_ids)), encoding="utf-8")
    return ids_path


def write_download_urls_file(output_folder: Path, collection_name: str, records: list[BeatmapRecord]) -> Path:
    output_folder.mkdir(parents=True, exist_ok=True)
    urls_path = output_folder / f"{safe_collection_filename(collection_name)}.download_urls.txt"
    urls = sorted({record.beatmapset_url for record in records})
    urls_path.write_text(chr(10).join(urls), encoding="utf-8")
    return urls_path


def write_7bit_encoded_int(stream: io.BytesIO, value: int) -> None:
    # .NET BinaryWriter string length format.
    value &= 0xFFFFFFFF
    while value >= 0x80:
        stream.write(bytes([(value | 0x80) & 0xFF]))
        value >>= 7
    stream.write(bytes([value & 0xFF]))


def write_dotnet_string(stream: io.BytesIO, value: str | None) -> None:
    encoded = (value or "").encode("utf-8")
    write_7bit_encoded_int(stream, len(encoded))
    stream.write(encoded)


def write_int32(stream: io.BytesIO, value: int) -> None:
    stream.write(struct.pack("<i", int(value)))


def write_byte(stream: io.BytesIO, value: int) -> None:
    stream.write(struct.pack("B", int(value) & 0xFF))


def write_double(stream: io.BytesIO, value: float) -> None:
    stream.write(struct.pack("<d", float(value)))


def current_oadate() -> float:
    # Same epoch used by .NET DateTime.ToOADate: 1899-12-30.
    epoch = datetime(1899, 12, 30, tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - epoch
    return delta.days + (delta.seconds + delta.microseconds / 1_000_000) / 86400


def write_api_osdb(output_folder: Path, collection_name: str, records: list[BeatmapRecord]) -> Path:
    """Write a Collection Manager .osdb directly from osu!api metadata.

    This writes the older uncompressed o!dm6 variant of .osdb. Collection Manager still
    supports it, and it avoids GZip-archive edge cases while preserving map IDs,
    mapset IDs, artist/title/difficulty, comments, mode, and star rating.
    """
    output_folder.mkdir(parents=True, exist_ok=True)
    output_path = output_folder / f"{safe_collection_filename(collection_name)}.osdb"

    sorted_records = sorted(records, key=lambda r: (r.artist.lower(), r.title.lower(), r.difficulty.lower(), r.map_id))

    stream = io.BytesIO()
    write_dotnet_string(stream, "o!dm6")
    write_double(stream, current_oadate())
    write_dotnet_string(stream, APP_NAME)
    write_int32(stream, 1)  # one collection

    write_dotnet_string(stream, collection_name)
    write_int32(stream, len(sorted_records))

    for record in sorted_records:
        write_int32(stream, record.map_id)
        write_int32(stream, record.mapset_id)
        write_dotnet_string(stream, record.artist)
        write_dotnet_string(stream, record.title)
        write_dotnet_string(stream, record.difficulty)
        write_dotnet_string(stream, record.md5)
        write_dotnet_string(stream, "")  # user comment
        write_byte(stream, record.play_mode)
        write_double(stream, record.stars)

    write_int32(stream, 0)  # hash-only maps
    write_dotnet_string(stream, "By Piotrekol")

    output_path.write_bytes(stream.getvalue())
    return output_path


def write_run_summary(
    output_folder: Path,
    collection_name: str,
    user: dict[str, Any],
    options: FetchOptions,
    stats: dict[str, int],
    beatmap_count: int,
    ids_path: Path,
    osdb_path: Path | None = None,
) -> Path:
    summary_path = output_folder / f"{safe_collection_filename(collection_name)}.summary.json"
    data = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "mapper": {
            "id": user.get("id"),
            "username": user.get("username"),
        },
        "collection_name": collection_name,
        "selected_categories": options.selected_types,
        "ruleset": options.ruleset or "all",
        "include_all_diffs_in_hosted_sets": options.include_all_diffs_in_hosted_sets,
        "include_guest_sets": options.include_guest_sets,
        "star_range": [options.star_min, options.star_max],
        "ar_range": [options.ar_min, options.ar_max],
        "category_counts": stats,
        "unique_beatmap_count": beatmap_count,
        "ids_file": str(ids_path),
        "osdb_file": str(osdb_path) if osdb_path else None,
    }
    summary_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return summary_path


def run_collection_manager(options: FetchOptions, ids_path: Path, log) -> Path:
    if not options.cm_cli_path:
        raise ValueError("Collection Manager CLI path is missing.")

    output_path = options.output_folder / f"{safe_collection_filename(options.collection_name)}.osdb"

    cmd = [
        str(options.cm_cli_path),
        "create",
        "-b",
        str(ids_path),
        "-o",
        str(output_path),
    ]

    if options.osu_location:
        cmd.extend(["-l", str(options.osu_location)])

    log("Running Collection Manager CLI...")
    log(" ".join(f'"{x}"' if " " in x else x for x in cmd))

    process = subprocess.run(cmd, capture_output=True, text=True)

    if process.stdout:
        log(process.stdout.strip())
    if process.stderr:
        log(process.stderr.strip())

    if process.returncode != 0:
        raise RuntimeError(f"Collection Manager CLI failed with exit code {process.returncode}")

    return output_path


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("980x720")
        self.minsize(860, 620)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None

        self.client_id_var = tk.StringVar()
        self.client_secret_var = tk.StringVar()
        self.mapper_var = tk.StringVar(value="JayAreEee")
        self.collection_name_var = tk.StringVar(value="JayAreEee - mapper maps")
        self.output_folder_var = tk.StringVar(value=str(Path.cwd() / "output"))
        self.cm_cli_var = tk.StringVar()
        self.osu_location_var = tk.StringVar()
        self.ruleset_var = tk.StringVar(value="All modes")
        self.star_min_var = tk.DoubleVar(value=0.0)
        self.star_max_var = tk.DoubleVar(value=15.0)
        self.ar_min_var = tk.DoubleVar(value=0.0)
        self.ar_max_var = tk.DoubleVar(value=11.0)
        self.include_all_diffs_var = tk.BooleanVar(value=True)
        self.include_guest_var = tk.BooleanVar(value=True)
        self.generate_osdb_var = tk.BooleanVar(value=True)
        self.save_secret_var = tk.BooleanVar(value=False)

        self.type_vars = {name: tk.BooleanVar(value=True) for name in BEATMAPSET_TYPES}

        self._build_ui()
        self._load_config()
        self.after(100, self._drain_log_queue)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        title = ttk.Label(root, text=APP_NAME, font=("Segoe UI", 18, "bold"))
        title.pack(anchor="w")

        subtitle = ttk.Label(
            root,
            text="Fetch a mapper's beatmaps with osu!api and generate an .osdb through Collection Manager CLI.",
        )
        subtitle.pack(anchor="w", pady=(0, 12))

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)

        setup_tab = ttk.Frame(notebook, padding=12)
        filters_tab = ttk.Frame(notebook, padding=12)
        run_tab = ttk.Frame(notebook, padding=12)
        about_tab = ttk.Frame(notebook, padding=12)

        notebook.add(setup_tab, text="Setup")
        notebook.add(filters_tab, text="Mapper + Filters")
        notebook.add(run_tab, text="Run")
        notebook.add(about_tab, text="Tool Outline")

        self._build_setup_tab(setup_tab)
        self._build_filters_tab(filters_tab)
        self._build_run_tab(run_tab)
        self._build_about_tab(about_tab)

    def _build_setup_tab(self, parent: ttk.Frame) -> None:
        grid = ttk.Frame(parent)
        grid.pack(fill="x")

        self._entry_row(grid, 0, "osu! OAuth client ID", self.client_id_var)
        self._entry_row(grid, 1, "osu! OAuth client secret", self.client_secret_var, show="*")

        ttk.Checkbutton(
            grid,
            text="Save client secret in local config file",
            variable=self.save_secret_var,
        ).grid(row=2, column=1, sticky="w", pady=(0, 12))

        self._path_row(grid, 3, "Collection Manager CLI folder (optional)", self.cm_cli_var, kind="folder")
        self._path_row(grid, 4, "Output folder", self.output_folder_var, kind="folder")

        for i in range(4):
            grid.columnconfigure(i, weight=1 if i == 1 else 0)

        note = ttk.Label(
            parent,
            wraplength=850,
            text=(
                "This version writes the .osdb directly from osu!api metadata, so Collection Manager CLI "
                "is optional. You can still save its folder here for convenience/future export steps."
            ),
        )
        note.pack(anchor="w", pady=(16, 0))

    def _build_filters_tab(self, parent: ttk.Frame) -> None:
        form = ttk.Frame(parent)
        form.pack(fill="x")

        self._entry_row(form, 0, "Mapper username or user ID", self.mapper_var)
        self._entry_row(form, 1, "Collection name", self.collection_name_var)

        ttk.Label(form, text="Ruleset").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Combobox(
            form,
            textvariable=self.ruleset_var,
            values=list(RULESETS.keys()),
            state="readonly",
        ).grid(row=2, column=1, sticky="ew", pady=6)

        for i in range(2):
            form.columnconfigure(i, weight=1 if i == 1 else 0)

        types_box = ttk.LabelFrame(parent, text="Beatmapset categories", padding=10)
        types_box.pack(fill="x", pady=12)

        for idx, bm_type in enumerate(BEATMAPSET_TYPES):
            ttk.Checkbutton(types_box, text=bm_type, variable=self.type_vars[bm_type]).grid(
                row=idx // 3, column=idx % 3, sticky="w", padx=10, pady=4
            )

        scope_box = ttk.LabelFrame(parent, text="Scope", padding=10)
        scope_box.pack(fill="x", pady=12)

        ttk.Checkbutton(
            scope_box,
            text="For hosted mapsets, include every difficulty in the set",
            variable=self.include_all_diffs_var,
        ).pack(anchor="w", pady=4)

        ttk.Checkbutton(
            scope_box,
            text="Include guest-difficulty mapsets",
            variable=self.include_guest_var,
        ).pack(anchor="w", pady=4)

        range_box = ttk.LabelFrame(parent, text="Range filters", padding=10)
        range_box.pack(fill="x", pady=12)
        self._range_slider(range_box, 0, "Star difficulty", self.star_min_var, self.star_max_var, 0.0, 15.0, "★")
        self._range_slider(range_box, 1, "Approach Rate", self.ar_min_var, self.ar_max_var, 0.0, 11.0, "AR")

        explanation = ttk.Label(
            parent,
            wraplength=850,
            text=(
                        "Recommended default: include ranked/loved/pending/graveyard/guest, include every difficulty in hosted sets, "
                "and leave star/AR ranges wide open. Use 'only mapper-owned difficulties' when you want something closer "
                "to osu!alternative's creator filter."
            ),
        )
        explanation.pack(anchor="w", pady=(8, 0))

    def _build_run_tab(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent)
        controls.pack(fill="x")

        ttk.Checkbutton(
            controls,
            text="Generate .osdb after fetching IDs",
            variable=self.generate_osdb_var,
        ).pack(side="left")

        ttk.Button(controls, text="Save Settings", command=self._save_config).pack(side="right", padx=4)
        ttk.Button(controls, text="Preview / Count IDs", command=lambda: self._start_worker(generate=False)).pack(side="right", padx=4)
        ttk.Button(controls, text="Build .osdb", command=lambda: self._start_worker(generate=True)).pack(side="right", padx=4)

        self.progress = ttk.Progressbar(parent, mode="indeterminate")
        self.progress.pack(fill="x", pady=12)

        self.log_text = tk.Text(parent, height=24, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

        ttk.Button(parent, text="Clear Log", command=self._clear_log).pack(anchor="e", pady=(8, 0))

    def _build_about_tab(self, parent: ttk.Frame) -> None:
        text = tk.Text(parent, wrap="word", height=30)
        text.pack(fill="both", expand=True)
        text.insert(
            "1.0",
            """
Tool outline + quick instructions
=================================

Core job
--------
Create an .osdb collection containing maps from a specific mapper without relying on Discord upload limits.

How to operate it
-----------------
0. Install uv if you do not already have it.
   - Windows: winget install --id=astral-sh.uv -e
   - macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh

1. Run the app in a contained uv environment:
   uv run mapper_osdb_gui.py

2. Optional, for repeatable dependency resolution:
   uv lock --script mapper_osdb_gui.py
   uv run --script mapper_osdb_gui.py

3. Open the Setup tab.
2. Enter your osu! OAuth client ID and client secret.
3. Optional: select your Collection Manager CLI folder.
4. Pick an output folder. By default, this is an output folder beside the script.
8. Open Mapper + Filters.
9. Enter the mapper username or user ID.
10. Choose categories, ruleset, scope, star range, and AR range.
11. Open Run.
12. Click Preview / Count IDs first.
13. If the count looks right, click Build .osdb.
14. Import the created .osdb into Collection Manager.

Generated files
---------------
1. <collection>.beatmap_ids.txt
   - Raw beatmap IDs used to build the collection.

2. <collection>.download_urls.txt
   - Unique beatmapset links for manual checking/downloading.

3. <collection>.osdb
   - API-filled Collection Manager collection file, if Build .osdb succeeds.
   - This is the main file to import into Collection Manager.

4. <collection>.summary.json
   - Run metadata: mapper, filters, category counts, and output paths.

Recommended default
-------------------
Use all five categories, All modes, include guest sets, include every difficulty in hosted mapsets, Star 0.0–15.0, and AR 0.0–11.0. This gives the broadest “all maps from this mapper” style output.

Main inputs
-----------
1. osu! OAuth client ID and client secret
   - Needed for osu!api v2 public data.

2. Mapper username or user ID
   - Example: JayAreEee
   - Username lookup prefixes the username with @ behind the scenes.

3. Beatmapset categories
   - ranked
   - loved
   - pending
   - graveyard
   - guest

4. Scope mode
   - Hosted set mode: include every difficulty inside mapsets hosted by the mapper.
   - Mapper-owned difficulty mode: include only difficulties whose owners include the mapper.

5. Ruleset filter
   - All modes
   - osu!
   - taiko
   - catch
   - mania

6. Range filters
   - Star difficulty minimum and maximum
   - Approach Rate minimum and maximum

7. Output folder and collection name
   - Writes <collection>.beatmap_ids.txt
   - Optionally writes <collection>.osdb

7. Collection Manager CLI folder
   - Optional.
   - The script now writes an API-filled .osdb directly to avoid ID-only placeholder entries.
   - The folder path is saved for convenience/future export steps.

8. osu!.db / client.realm path
   - No longer required by the main builder.

Actions
-------
1. Preview / Count IDs
   - Fetches the mapper's mapsets.
   - Deduplicates beatmap IDs.
   - Writes a beatmap_ids.txt file.
   - Does not create an .osdb.

2. Build .osdb
   - Does everything above.
   - Writes an API-filled .osdb directly.

3. Save Settings
   - Saves non-secret settings by default.
   - Can save the client secret only if the checkbox is enabled.

What this tool deliberately does not do yet
------------------------------------------
1. It does not download .osz files directly.
2. It does not automate osu!alternative Discord commands.
3. It does not merge multiple .osdb files yet.

uv notes
--------
The script contains inline dependency metadata at the top. uv reads that metadata, creates/uses an isolated environment, installs requests there, and then runs the GUI. This means you do not need to install requests globally.

Good next features
------------------
1. Add chunking: split giant mappers into several .osdb files by status or star range.
2. Add a local cache so repeated runs do not re-fetch everything.
3. Add a direct .osz download queue if you want a full map pack builder.
4. Add presets for “hosted sets only”, “GD only”, and “osu!alternative-like creator mode”.
""".strip(),
        )
        text.configure(state="disabled")

    def _entry_row(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar, show: str | None = None) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 10))
        ttk.Entry(parent, textvariable=var, show=show).grid(row=row, column=1, sticky="ew", pady=6)

    def _range_slider(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        min_var: tk.DoubleVar,
        max_var: tk.DoubleVar,
        lower: float,
        upper: float,
        prefix: str,
    ) -> None:
        label_var = tk.StringVar()

        def update_label(*_args) -> None:
            lo, hi = sorted((float(min_var.get()), float(max_var.get())))
            label_var.set(f"{label}: {prefix} {lo:.1f} – {hi:.1f}")

        row_frame = ttk.Frame(parent)
        row_frame.grid(row=row, column=0, sticky="ew", pady=8)
        row_frame.columnconfigure(1, weight=1)
        row_frame.columnconfigure(3, weight=1)

        ttk.Label(row_frame, textvariable=label_var, width=34).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Label(row_frame, text="Min").grid(row=0, column=1, sticky="w")
        ttk.Scale(row_frame, from_=lower, to=upper, variable=min_var, command=update_label).grid(
            row=1, column=1, sticky="ew", padx=(0, 12)
        )
        ttk.Label(row_frame, text="Max").grid(row=0, column=3, sticky="w")
        ttk.Scale(row_frame, from_=lower, to=upper, variable=max_var, command=update_label).grid(
            row=1, column=3, sticky="ew"
        )

        min_var.trace_add("write", update_label)
        max_var.trace_add("write", update_label)
        update_label()

    def _path_row(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar, kind: str) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 10))
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", pady=6)

        if kind == "file":
            ttk.Button(parent, text="Select file", command=lambda: self._browse_file(var)).grid(
                row=row, column=2, sticky="ew", padx=(8, 0), pady=6
            )
        elif kind == "folder":
            ttk.Button(parent, text="Select folder", command=lambda: self._browse_folder(var)).grid(
                row=row, column=2, sticky="ew", padx=(8, 0), pady=6
            )
        else:
            raise ValueError(f"Unknown path row kind: {kind}")

    def _browse_file(self, var: tk.StringVar) -> None:
        selected = filedialog.askopenfilename(parent=self)
        if selected:
            var.set(selected)
            self.lift()
            self.focus_force()

    def _browse_folder(self, var: tk.StringVar) -> None:
        selected = filedialog.askdirectory(parent=self, mustexist=True)
        if selected:
            var.set(selected)
            self.lift()
            self.focus_force()

    def _build_fetch_options(self, force_generate: bool) -> FetchOptions:
        selected_types = [name for name, var in self.type_vars.items() if var.get()]
        if not selected_types:
            raise ValueError("Select at least one beatmapset category.")

        output_folder = Path(self.output_folder_var.get()).expanduser()
        cm_cli = Path(self.cm_cli_var.get()).expanduser() if self.cm_cli_var.get().strip() else None
        osu_location = Path(self.osu_location_var.get()).expanduser() if self.osu_location_var.get().strip() else None

        generate_osdb = force_generate and self.generate_osdb_var.get()

        client_id = self.client_id_var.get().strip()
        client_secret = self.client_secret_var.get().strip()
        if not client_id:
            raise ValueError("osu! OAuth client ID is required.")
        if not client_secret:
            raise ValueError("osu! OAuth client secret is required.")
        if not client_id.isdigit():
            raise ValueError("osu! OAuth client ID should be numeric.")

        mapper = self.mapper_var.get().strip()
        if not mapper:
            raise ValueError("Mapper username or user ID is required.")

        collection_name = self.collection_name_var.get().strip() or f"{mapper} - mapper maps"
        star_min, star_max = sorted((float(self.star_min_var.get()), float(self.star_max_var.get())))
        ar_min, ar_max = sorted((float(self.ar_min_var.get()), float(self.ar_max_var.get())))

        return FetchOptions(
            client_id=client_id,
            client_secret=client_secret,
            mapper=mapper,
            selected_types=selected_types,
            ruleset=RULESETS[self.ruleset_var.get()],
            include_all_diffs_in_hosted_sets=self.include_all_diffs_var.get(),
            include_guest_sets=self.include_guest_var.get(),
            star_min=round(star_min, 2),
            star_max=round(star_max, 2),
            ar_min=round(ar_min, 2),
            ar_max=round(ar_max, 2),
            output_folder=output_folder,
            collection_name=collection_name,
            cm_cli_path=cm_cli,
            osu_location=osu_location,
            generate_osdb=generate_osdb,
        )

    def _start_worker(self, generate: bool) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "A job is already running.")
            return

        try:
            options = self._build_fetch_options(force_generate=generate)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self._clear_log()
        self.progress.start(10)
        self.worker = threading.Thread(target=self._run_job, args=(options,), daemon=True)
        self.worker.start()

    def _run_job(self, options: FetchOptions) -> None:
        try:
            self.log("Starting job...")
            user, beatmap_records, stats = collect_beatmap_ids(options, self.log)

            if not beatmap_records:
                self.log("No beatmaps found with these filters.")
                return

            records = list(beatmap_records.values())
            ids_path = write_ids_file(options.output_folder, options.collection_name, set(beatmap_records.keys()))
            self.log(f"Wrote IDs file: {ids_path}")

            urls_path = write_download_urls_file(options.output_folder, options.collection_name, records)
            self.log(f"Wrote beatmapset URL list: {urls_path}")

            self.log("Category counts from this run:")
            for key, value in stats.items():
                self.log(f"  {key}: {value}")

            output_path = None
            if options.generate_osdb:
                output_path = write_api_osdb(options.output_folder, options.collection_name, records)
                self.log(f"Created API-filled .osdb: {output_path}")
            else:
                self.log("Preview complete. .osdb generation skipped.")

            summary_path = write_run_summary(
                options.output_folder,
                options.collection_name,
                user,
                options,
                stats,
                len(beatmap_records),
                ids_path,
                output_path,
            )
            self.log(f"Wrote run summary: {summary_path}")
            self.log("Done.")
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            self.log("__STOP_PROGRESS__")

    def log(self, message: str) -> None:
        self.log_queue.put(message)

    def _drain_log_queue(self) -> None:
        try:
            while True:
                message = self.log_queue.get_nowait()
                if message == "__STOP_PROGRESS__":
                    self.progress.stop()
                else:
                    self._append_log(message)
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _save_config(self) -> None:
        data = {
            "client_id": self.client_id_var.get(),
            "client_secret": self.client_secret_var.get() if self.save_secret_var.get() else "",
            "mapper": self.mapper_var.get(),
            "collection_name": self.collection_name_var.get(),
            "output_folder": self.output_folder_var.get(),
            "cm_cli": self.cm_cli_var.get(),
            "osu_location": self.osu_location_var.get(),
            "ruleset": self.ruleset_var.get(),
            "star_min": self.star_min_var.get(),
            "star_max": self.star_max_var.get(),
            "ar_min": self.ar_min_var.get(),
            "ar_max": self.ar_max_var.get(),
            "include_all_diffs": self.include_all_diffs_var.get(),
            "include_guest": self.include_guest_var.get(),
            "generate_osdb": self.generate_osdb_var.get(),
            "save_secret": self.save_secret_var.get(),
            "types": {key: var.get() for key, var in self.type_vars.items()},
        }
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        messagebox.showinfo(APP_NAME, f"Saved settings to {CONFIG_PATH}")

    def _load_config(self) -> None:
        if not CONFIG_PATH.exists():
            return

        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return

        self.client_id_var.set(data.get("client_id", ""))
        self.client_secret_var.set(data.get("client_secret", ""))
        self.mapper_var.set(data.get("mapper", self.mapper_var.get()))
        self.collection_name_var.set(data.get("collection_name", self.collection_name_var.get()))
        self.output_folder_var.set(data.get("output_folder", self.output_folder_var.get()))
        self.cm_cli_var.set(data.get("cm_cli", ""))
        self.osu_location_var.set(data.get("osu_location", ""))
        self.ruleset_var.set(data.get("ruleset", "All modes"))
        self.star_min_var.set(float(data.get("star_min", 0.0)))
        self.star_max_var.set(float(data.get("star_max", 15.0)))
        self.ar_min_var.set(float(data.get("ar_min", 0.0)))
        self.ar_max_var.set(float(data.get("ar_max", 11.0)))
        self.include_all_diffs_var.set(data.get("include_all_diffs", True))
        self.include_guest_var.set(data.get("include_guest", True))
        self.generate_osdb_var.set(data.get("generate_osdb", True))
        self.save_secret_var.set(data.get("save_secret", False))

        for key, value in data.get("types", {}).items():
            if key in self.type_vars:
                self.type_vars[key].set(bool(value))


if __name__ == "__main__":
    app = App()
    app.mainloop()
