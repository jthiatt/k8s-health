"""Build the coverage site that CI publishes to GitHub Pages: an HTML report per component, a shields.io
endpoint badge per component, and an index. Usage: python coverage_site.py <output dir>
(run from the repo root after the CI test steps, which leave .coverage.agent and .coverage.page)."""
import html
import json
import pathlib
import subprocess
import sys

COMPONENTS = [("agent", ".coverage.agent", "Agent"), ("status-page", ".coverage.page", "Status page")]

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
rows = []
for key, data_file, title in COMPONENTS:
    subprocess.run(["coverage", "html", f"--data-file={data_file}", "-d", str(out / key),
                    "--title", f"k8s-health {title} coverage"], check=True)
    report = out / f"{key}.json"
    subprocess.run(["coverage", "json", f"--data-file={data_file}", "-o", str(report)], check=True)
    pct = json.loads(report.read_text())["totals"]["percent_covered"]
    color = "brightgreen" if pct >= 95 else "green" if pct >= 90 else "red"
    (out / f"badge-{key}.json").write_text(json.dumps(
        {"schemaVersion": 1, "label": f"{key} coverage", "message": f"{int(pct)}%", "color": color}))
    rows.append(f'<li><a href="{key}/index.html">{html.escape(title)}</a>: {pct:.1f}% of lines</li>')

(out / "index.html").write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>k8s-health coverage</title></head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 16px">
<h1>k8s-health code coverage</h1><ul>{''.join(rows)}</ul>
<p>Built by CI from <code>main</code>. Each component must stay at 90% or more.
<a href="https://github.com/jthiatt/k8s-health">Back to the repository</a></p></body></html>
""")
print("\n".join(rows))
