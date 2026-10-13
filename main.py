import os
from typing import Any
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()
TOKEN = os.getenv("SPORTMONKS_TOKEN", "").strip()
BASE = "https://api.sportmonks.com/v3/football"

app = FastAPI(title="Canlı Gol Sinyali API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Üretimde kendi uygulama/web alan adınızla sınırlandırın.
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True, "token_configured": bool(TOKEN)}

async def sportmonks_get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    if not TOKEN:
        raise HTTPException(status_code=503, detail="Sunucuda SPORTMONKS_TOKEN ayarlanmamış.")
    headers = {"Authorization": TOKEN, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.get(f"{BASE}/{path}", params=params or {}, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"Futbol veri sağlayıcısı HTTP {exc.response.status_code} döndürdü.") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Canlı maç sağlayıcısına ulaşılamadı.") from exc

def name_of(item: dict) -> str:
    return str(item.get("name") or item.get("type", {}).get("name") or "").lower()

def stat_value(stats: list, terms: tuple[str, ...]) -> float | None:
    for item in stats or []:
        n = name_of(item)
        if any(term in n for term in terms):
            value = item.get("data", {}).get("value") if isinstance(item.get("data"), dict) else item.get("value")
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None

def participants(fixture: dict) -> list[str]:
    out = []
    for p in fixture.get("participants") or []:
        name = p.get("name")
        if name:
            out.append(str(name))
    if out:
        return out[:2]
    raw = fixture.get("name")
    return [str(raw)] if raw else ["Takımlar yüklenemedi"]

def score_of(fixture: dict) -> str:
    scores = fixture.get("scores") or []
    vals = []
    for s in scores:
        description = str(s.get("description", "")).lower()
        if description in ("current", "2nd-half", "1st-half", "ft", "fulltime"):
            score = s.get("score") or {}
            value = score.get("goals")
            if value is not None:
                vals.append((description, value))
    if vals:
        # Prefer current score if provided.
        vals.sort(key=lambda x: 0 if x[0] == "current" else 1)
        return str(vals[0][1])
    return "—"

def minute_of(fixture: dict) -> int | None:
    # Provider state/period fields differ by plan and endpoint; only use explicit minute values.
    for period in fixture.get("periods") or []:
        value = period.get("minutes") or period.get("minute")
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    for event in reversed(fixture.get("events") or []):
        value = event.get("minute")
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return None

def analyze(fixture: dict) -> dict:
    stats = fixture.get("statistics") or []
    minute = minute_of(fixture)
    shots = stat_value(stats, ("shots total", "total shots", "shots"))
    on_target = stat_value(stats, ("shots on target", "shots on goal"))
    corners = stat_value(stats, ("corners",))
    # Conservative heuristic: only award points for fields actually supplied.
    points = 0
    known = 0
    if minute is not None:
        known += 1
        if minute >= 55: points += 1
        if minute >= 70: points += 1
    if shots is not None:
        known += 1
        if shots >= 16: points += 2
        elif shots >= 10: points += 1
    if on_target is not None:
        known += 1
        if on_target >= 6: points += 2
        elif on_target >= 3: points += 1
    if corners is not None:
        known += 1
        if corners >= 8: points += 1

    if known < 2:
        level = "VERİ EKSİK"
        label = "Yeterli canlı istatistik yok"
    elif points >= 4:
        level = "YÜKSEK"
        label = "Birden fazla gol destekleyici işaret"
    elif points >= 2:
        level = "ORTA"
        label = "Golü destekleyebilecek işaretler var"
    else:
        level = "DÜŞÜK"
        label = "Sınırlı işaret"

    return {
        "minute": minute,
        "signal": level,
        "signal_label": label,
        "indicator_points": points,
        "stats_available": {"shots": shots, "shots_on_target": on_target, "corners": corners},
        "note": "Bu kural tabanlı gösterge gol olasılığı veya garanti değildir."
    }

@app.get("/matches")
async def matches():
    payload = await sportmonks_get(
        "livescores/inplay",
        {"include": "participants;scores;events;statistics"}
    )
    data = payload.get("data") or []
    result = []
    for fixture in data:
        result.append({
            "id": fixture.get("id"),
            "league_id": fixture.get("league_id"),
            "match": " - ".join(participants(fixture)),
            "score": score_of(fixture),
            "minute": minute_of(fixture),
            "analysis": analyze(fixture),
        })
    return {"count": len(result), "matches": result}
