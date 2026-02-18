import requests

def maybe_notify_discord(http: requests.Session, webhook_url: str, title: str, msg: str):
    if not webhook_url:
        return
    payload = {"content": f"**{title}**\n{msg}"}
    try:
        http.post(webhook_url, json=payload, timeout=10).raise_for_status()
    except Exception:
        pass
