"""
12_export_trial_dashboard.py — Cardiac-Regenerative-Lab-Autocrawler
Virelion Biotech

Reads raw_trials.json (step 3's output) and generates a single,
self-contained HTML dashboard: phase distribution, top sponsors, geography,
and trial starts by year. Uses Chart.js via CDN — no server, no build step,
just open the file in a browser. Consistent with the pipeline's "plain
files, no orchestration" philosophy; this is the one output that's HTML
instead of CSV/JSON, because "visualize this" is what was actually asked for.

Reads from raw_trials.json rather than labs_final.json deliberately: trial
records here are per-trial, not per-PI, and phase/sponsor/geography
breakdowns are most meaningful counted at the trial level before
entity resolution collapses multiple trials into one lab profile.

Output: data/<year>/trial_dashboard.html

Usage:
    python 12_export_trial_dashboard.py
"""

import json
from collections import Counter

import config


def _year_from_date(date_str: str) -> str:
    return date_str[:4] if date_str and date_str[:4].isdigit() else "Unknown"


def build_dashboard_data(trials: list[dict]) -> dict:
    phase_counts = Counter(t.get("phase") or "Not specified" for t in trials)

    sponsor_counts = Counter(t.get("lead_sponsor") or "Unknown" for t in trials)
    top_sponsors = sponsor_counts.most_common(15)

    country_counts = Counter()
    for t in trials:
        countries = {loc.get("country") for loc in t.get("locations", []) if loc.get("country")}
        for c in countries:
            country_counts[c] += 1
    top_countries = country_counts.most_common(15)

    year_counts = Counter(_year_from_date(t.get("start_date", "")) for t in trials)
    years_sorted = sorted((y for y in year_counts if y != "Unknown"))

    status_counts = Counter(t.get("status") or "Unknown" for t in trials)

    return {
        "total_trials": len(trials),
        "phase_labels": list(phase_counts.keys()),
        "phase_values": list(phase_counts.values()),
        "sponsor_labels": [s[0] for s in top_sponsors],
        "sponsor_values": [s[1] for s in top_sponsors],
        "country_labels": [c[0] for c in top_countries],
        "country_values": [c[1] for c in top_countries],
        "year_labels": years_sorted,
        "year_values": [year_counts[y] for y in years_sorted],
        "status_labels": list(status_counts.keys()),
        "status_values": list(status_counts.values()),
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Cardiac Regeneration Clinical Trial Landscape — {year}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0;
          background: #f7f7f5; color: #2b2b2b; }}
  header {{ padding: 24px 32px; background: #ffffff; border-bottom: 1px solid #e5e5e2; }}
  header h1 {{ margin: 0 0 4px 0; font-size: 22px; }}
  header p {{ margin: 0; color: #666; font-size: 14px; }}
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; padding: 24px 32px; }}
  .card {{ background: #ffffff; border: 1px solid #e5e5e2; border-radius: 10px; padding: 18px; }}
  .card h2 {{ margin: 0 0 12px 0; font-size: 15px; color: #444; }}
  .full-width {{ grid-column: 1 / -1; }}
  canvas {{ max-height: 340px; }}
</style>
</head>
<body>
<header>
  <h1>Cardiac Regeneration Clinical Trial Landscape</h1>
  <p>Virelion Biotech — {year} run &middot; {total_trials} trials from ClinicalTrials.gov</p>
</header>
<div class="grid">
  <div class="card"><h2>Trials by Phase</h2><canvas id="phaseChart"></canvas></div>
  <div class="card"><h2>Trials by Status</h2><canvas id="statusChart"></canvas></div>
  <div class="card full-width"><h2>Trial Starts by Year</h2><canvas id="yearChart"></canvas></div>
  <div class="card"><h2>Top Sponsors</h2><canvas id="sponsorChart"></canvas></div>
  <div class="card"><h2>Top Countries</h2><canvas id="countryChart"></canvas></div>
</div>
<script>
const data = {data_json};

function bar(id, labels, values, horizontal) {{
  new Chart(document.getElementById(id), {{
    type: 'bar',
    data: {{ labels: labels, datasets: [{{ data: values, backgroundColor: '#c65d3b' }}] }},
    options: {{
      indexAxis: horizontal ? 'y' : 'x',
      plugins: {{ legend: {{ display: false }} }},
      scales: {{ y: {{ beginAtZero: true }} }}
    }}
  }});
}}

bar('phaseChart', data.phase_labels, data.phase_values, false);
bar('statusChart', data.status_labels, data.status_values, false);
bar('yearChart', data.year_labels, data.year_values, false);
bar('sponsorChart', data.sponsor_labels, data.sponsor_values, true);
bar('countryChart', data.country_labels, data.country_values, true);
</script>
</body>
</html>
"""


def main():
    config.ensure_run_dir()

    in_path = config.run_path("raw_trials")
    if not in_path.exists():
        print(f"{in_path.name} not found — run step 3 first.")
        return

    with open(in_path) as f:
        trials = json.load(f)

    print(f"Building dashboard from {len(trials)} trial records...")
    dashboard_data = build_dashboard_data(trials)

    html = HTML_TEMPLATE.format(
        year=config.CURRENT_YEAR,
        total_trials=dashboard_data["total_trials"],
        data_json=json.dumps(dashboard_data),
    )

    out_path = config.run_path("trial_dashboard_html")
    with open(out_path, "w") as f:
        f.write(html)

    print(f"Done. Wrote dashboard to {out_path} — open it directly in a browser.")


if __name__ == "__main__":
    main()
