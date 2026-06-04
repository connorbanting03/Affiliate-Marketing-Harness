import json
import os
import webbrowser
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, url_for

app = Flask(__name__)

LEADS_FILE = os.path.join(os.path.dirname(__file__), "leads.json")

# Known affiliate network domains → human-readable brand names
_KNOWN_DOMAINS: dict[str, str] = {
    "shopify.pxf.io": "Shopify",
    "shareasale.com": "ShareASale",
    "impact.com": "Impact",
    "awin1.com": "Awin",
    "clickbank.net": "ClickBank",
    "amazon.com": "Amazon",
    "go.fiverr.com": "Fiverr",
    "convertkit.com": "ConvertKit",
    "bluehost.com": "Bluehost",
    "hostgator.com": "HostGator",
}


def get_link_label(url: str) -> str:
    try:
        host = urlparse(url).hostname or url
        host = host.removeprefix("www.")
        return _KNOWN_DOMAINS.get(host, host)
    except Exception:
        return url


def load_leads() -> list[dict]:
    if os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "r") as f:
            return json.load(f)
    return []


def save_leads(leads: list[dict]) -> None:
    with open(LEADS_FILE, "w") as f:
        json.dump(leads, f, indent=2)


def _group_leads(leads: list[dict]) -> list[dict]:
    order = {"new": 0, "opened": 1, "done": 2}
    sorted_leads = sorted(leads, key=lambda l: order.get(l.get("status", "new"), 99))

    groups_map: dict[str, list[dict]] = {}
    for lead in sorted_leads:
        link = lead.get("affiliate_link", "Unknown")
        groups_map.setdefault(link, []).append(lead)

    groups = []
    for link, group_leads in groups_map.items():
        groups.append({
            "label": get_link_label(link),
            "affiliate_link": link,
            "leads": group_leads,
            "stats": {
                "total": len(group_leads),
                "new": sum(1 for l in group_leads if l.get("status", "new") == "new"),
                "opened": sum(1 for l in group_leads if l.get("status") == "opened"),
                "done": sum(1 for l in group_leads if l.get("status") == "done"),
            },
        })
    return groups


@app.route("/")
def index():
    leads = load_leads()
    stats = {
        "total": len(leads),
        "new": sum(1 for l in leads if l.get("status", "new") == "new"),
        "opened": sum(1 for l in leads if l.get("status") == "opened"),
        "done": sum(1 for l in leads if l.get("status") == "done"),
    }
    groups = _group_leads(leads)
    return render_template("index.html", groups=groups, stats=stats)


@app.route("/open/<lead_id>")
def open_lead(lead_id: str):
    leads = load_leads()
    for lead in leads:
        if lead["id"] == lead_id:
            if lead.get("status") == "new":
                lead["status"] = "opened"
                save_leads(leads)
            if lead.get("url"):
                webbrowser.open(lead["url"])
            break
    return redirect(url_for("index"))


@app.route("/done/<lead_id>")
def mark_done(lead_id: str):
    leads = load_leads()
    for lead in leads:
        if lead["id"] == lead_id:
            lead["status"] = "done"
            save_leads(leads)
            break
    return redirect(url_for("index"))


@app.route("/api/leads")
def api_leads():
    return jsonify(load_leads())


if __name__ == "__main__":
    print("Starting Affiliate Lead UI at http://localhost:5000")
    app.run(debug=True, port=5000)
