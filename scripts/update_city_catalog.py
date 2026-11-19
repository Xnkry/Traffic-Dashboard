import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile


SOURCE = "https://download.geonames.org/export/dump/cities500.zip"
REGIONS = {
    "01": "Tanger-Tétouan-Al Hoceïma", "02": "Oriental", "03": "Fès-Meknès",
    "04": "Rabat-Salé-Kénitra", "05": "Béni Mellal-Khénifra", "06": "Casablanca-Settat",
    "07": "Marrakech-Safi", "08": "Drâa-Tafilalet", "09": "Souss-Massa",
    "10": "Guelmim-Oued Noun", "11": "Laâyoune-Sakia El Hamra", "12": "Dakhla-Oued Ed-Dahab",
}
LEGACY_NAMES = {
    "2553604": "Casablanca", "2538475": "Rabat", "2542997": "Marrakech", "2530335": "Tangier",
    "2548885": "Fès", "2561668": "Agadir", "2542715": "Meknès", "2540483": "Oujda",
    "2544571": "Kénitra", "2528910": "Tétouan", "2550078": "El Jadida", "2537881": "Safi",
}


def extract(archive):
    with ZipFile(io.BytesIO(archive)) as zipped:
        records = csv.reader(io.StringIO(zipped.read("cities500.txt").decode("utf-8")), delimiter="\t")
        rows = [r for r in records if r[8] == "MA" and r[7] in {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLC"}]
    names = Counter(LEGACY_NAMES.get(r[0], r[1]) for r in rows)
    result = []
    for r in rows:
        name = LEGACY_NAMES.get(r[0], r[1])
        if names[name] > 1:
            name = f"{name} ({r[0]})"
        result.append({"geoname_id": r[0], "city": name, "region": REGIONS.get(r[10], "Unclassified"),
            "lat": r[4], "lon": r[5], "population": r[14], "feature_code": r[7],
            "country_code": r[8], "source_updated": r[18]})
    return sorted(result, key=lambda r: (-int(r["population"]), r["city"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.archive:
        archive = args.archive.read_bytes()
    else:
        with urlopen(SOURCE, timeout=120) as response:
            archive = response.read()
    rows = extract(archive)
    if len(rows) < 100 or len({r["geoname_id"] for r in rows}) != len(rows):
        raise ValueError("Unexpected source data; refusing to replace the catalog")
    root = Path(__file__).resolve().parents[1]
    with (root / "data/cities.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (root / "data/cities-source.json").write_text(json.dumps({
        "source": SOURCE, "license": "CC BY 4.0", "attribution": "GeoNames (www.geonames.org)",
        "retrieved_at": datetime.now(timezone.utc).isoformat(), "source_sha256": hashlib.sha256(archive).hexdigest(),
        "country_filter": "MA", "excluded_feature_codes": ["PPLX"], "places": len(rows),
        "note": "City and town dataset, including administrative seats; not an exhaustive register or a guarantee of traffic coverage."
    }, indent=2) + "\n")
    print(f"Saved {len(rows)} cities and towns")


if __name__ == "__main__":
    main()
