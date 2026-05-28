"""Пульт управления сегментами фидов sutochno-feeds.

Читает config/segments.yaml из GitHub, показывает форму редактирования,
коммитит изменения обратно и триггерит пересборку через GitHub Actions.
Списки городов/удобств подтягиваются из meta-файлов на gh-pages."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

import requests
import streamlit as st
import yaml


st.set_page_config(page_title="Пульт фидов", page_icon="🎛", layout="wide")


# ───────────────────────── secrets / config ─────────────────────────

def _secret(key: str, default: str | None = None) -> str:
    try:
        return st.secrets[key]
    except (KeyError, FileNotFoundError):
        if default is not None:
            return default
        st.error(f"Отсутствует секрет `{key}`. Добавь в Streamlit Cloud → App settings → Secrets.")
        st.stop()


APP_PASSWORD = _secret("app_password")
GITHUB_TOKEN = _secret("github_token")
GITHUB_REPO = _secret("github_repo")
GITHUB_BRANCH = _secret("github_branch", "main")
WORKFLOW_FILE = _secret("workflow_file", "daily.yml")
META_BASE_URL = _secret("meta_base_url")
# Feeds live next to meta on the same Pages host.
FEEDS_BASE_URL = META_BASE_URL.rsplit("/", 1)[0] + "/feeds"

SEGMENTS_PATH = "config/segments.yaml"


def feed_url(segment_id: str) -> str:
    return f"{FEEDS_BASE_URL}/{segment_id}.csv"


# ───────────────────────── auth ─────────────────────────

def login_gate() -> None:
    if st.session_state.get("authed"):
        return
    st.title("🎛 Пульт фидов")
    with st.form("login"):
        pwd = st.text_input("Пароль", type="password")
        if st.form_submit_button("Войти", type="primary"):
            if pwd == APP_PASSWORD:
                st.session_state.authed = True
                st.rerun()
            else:
                st.error("Неверный пароль")
    st.stop()


# ───────────────────────── GitHub API ─────────────────────────

GH = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def gh_get(path: str, **kw) -> requests.Response:
    return requests.get(f"{GH}/{path}", headers=HEADERS, timeout=30, **kw)


def gh_put(path: str, payload: dict) -> requests.Response:
    return requests.put(f"{GH}/{path}", headers=HEADERS, json=payload, timeout=30)


def gh_post(path: str, payload: dict) -> requests.Response:
    return requests.post(f"{GH}/{path}", headers=HEADERS, json=payload, timeout=30)


@st.cache_data(ttl=30)
def fetch_segments_file() -> tuple[dict, str]:
    """Returns (parsed_yaml, sha) — sha needed for PUT update."""
    r = gh_get(f"repos/{GITHUB_REPO}/contents/{SEGMENTS_PATH}",
               params={"ref": GITHUB_BRANCH})
    r.raise_for_status()
    payload = r.json()
    content = base64.b64decode(payload["content"]).decode("utf-8")
    return yaml.safe_load(content), payload["sha"]


def save_segments_file(data: dict, prev_sha: str, msg: str) -> str:
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8")
    r = gh_put(
        f"repos/{GITHUB_REPO}/contents/{SEGMENTS_PATH}",
        {
            "message": msg,
            "content": base64.b64encode(body).decode("ascii"),
            "sha": prev_sha,
            "branch": GITHUB_BRANCH,
        },
    )
    r.raise_for_status()
    fetch_segments_file.clear()
    return r.json()["commit"]["sha"]


def trigger_workflow() -> None:
    r = gh_post(
        f"repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        {"ref": GITHUB_BRANCH},
    )
    if r.status_code >= 300:
        raise RuntimeError(f"{r.status_code}: {r.text}")


@st.cache_data(ttl=20)
def latest_run() -> dict | None:
    r = gh_get(
        f"repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/runs",
        params={"per_page": 1},
    )
    if r.status_code >= 300:
        return None
    runs = r.json().get("workflow_runs", [])
    return runs[0] if runs else None


# ───────────────────────── meta lists ─────────────────────────

@st.cache_data(ttl=60)
def load_meta(name: str) -> list[dict]:
    try:
        r = requests.get(f"{META_BASE_URL}/{name}.json", timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


# ───────────────────────── UI helpers ─────────────────────────

def fmt_run_status(run: dict | None) -> str:
    if not run:
        return "пока не запускался"
    status = run.get("status")
    concl = run.get("conclusion")
    started = run.get("run_started_at") or run.get("created_at")
    when = ""
    if started:
        try:
            dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - dt
            mins = int(delta.total_seconds() // 60)
            when = f" · {mins} мин назад" if mins < 60 else f" · {mins // 60} ч назад"
        except ValueError:
            pass
    if status == "in_progress" or status == "queued":
        return f"⏳ идёт{when}"
    if concl == "success":
        return f"✅ успешно{when}"
    if concl == "failure":
        return f"❌ ошибка{when}"
    return f"{status}{when}"


def segment_summary(seg: dict) -> str:
    f = seg.get("filter", {})
    parts = []
    if dests := f.get("destination_in"):
        parts.append(f"📍 {', '.join(dests[:3])}{'…' if len(dests) > 3 else ''}")
    if (ms := f.get("min_score")) is not None:
        parts.append(f"⭐ ≥ {ms}")
    if stars := f.get("stars_in"):
        parts.append(f"🌟 {'/'.join(map(str, stars))}")
    if amens := f.get("amenities_any"):
        parts.append(f"🛎 {len(amens)} удобств")
    if (mp := f.get("max_price")) is not None:
        parts.append(f"≤ {mp} ₽")
    return " · ".join(parts) or "нет фильтров"


# ───────────────────────── main ─────────────────────────

def main() -> None:
    login_gate()

    st.title("🎛 Пульт фидов")
    st.caption(f"Репозиторий: `{GITHUB_REPO}` · ветка: `{GITHUB_BRANCH}`")

    try:
        cfg, sha = fetch_segments_file()
    except Exception as e:
        st.error(f"Не получилось прочитать segments.yaml: {e}")
        st.stop()

    segments = cfg.get("segments", [])

    # Top status bar
    run = latest_run()
    col_a, col_b = st.columns([3, 1])
    with col_a:
        st.markdown(f"**Последний прогон:** {fmt_run_status(run)}")
        segment_ids = [s.get("id") for s in segments if s.get("id")]
        if segment_ids:
            feed_links = ", ".join(f"`{sid}.csv`" for sid in segment_ids)
            st.markdown(f"**Собранные фиды:** {feed_links}")
    with col_b:
        if st.button("🔄 Обновить", help="Перечитать состояние"):
            fetch_segments_file.clear()
            latest_run.clear()
            load_meta.clear()
            st.rerun()

    st.divider()

    cities = load_meta("cities")
    amenities = load_meta("amenities")
    stars = load_meta("stars")

    # Toolbar
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("➕ Добавить сегмент"):
            new_seg = {
                "id": f"new-segment-{len(segments) + 1}",
                "description": "",
                "filter": {},
                "template": "premium_gold",
                "limit": 0,
            }
            cfg.setdefault("segments", []).append(new_seg)
            try:
                save_segments_file(cfg, sha, f"Add segment {new_seg['id']}")
                st.success("Сегмент добавлен. Открой его ниже, чтобы настроить.")
                st.rerun()
            except Exception as e:
                st.error(f"Не сохранилось: {e}")

    with col2:
        if st.button("▶️ Запустить пересборку всех сегментов", type="primary"):
            try:
                trigger_workflow()
                latest_run.clear()
                st.success("Запустил. Проверяй статус сверху через минуту.")
            except Exception as e:
                st.error(f"Не получилось запустить: {e}")

    st.divider()

    if not segments:
        st.info("Сегментов пока нет. Нажми «Добавить сегмент» наверху.")
        return

    for idx, seg in enumerate(segments):
        with st.expander(f"**{seg.get('id', '?')}** — {segment_summary(seg)}", expanded=False):
            render_segment_form(idx, seg, cfg, sha, cities, amenities, stars)


def render_segment_form(
    idx: int, seg: dict, cfg: dict, sha: str,
    cities: list[dict], amenities: list[dict], stars: list[dict],
) -> None:
    flt = seg.get("filter", {})
    seg_id = seg.get("id", "")

    if seg_id:
        st.markdown("**Ссылка на фид для Яндекс.Директа:**")
        st.code(feed_url(seg_id), language=None)
        st.caption(
            "Скопируй и вставь в Директ → Источники → Фиды. "
            "Файл появится по этому адресу после первой успешной пересборки сегмента."
        )

    with st.form(f"seg-{idx}"):
        col1, col2 = st.columns(2)
        with col1:
            new_id = st.text_input("ID сегмента", value=seg.get("id", ""),
                                   help="латиницей, без пробелов — войдёт в имя выходного файла")
            new_desc = st.text_input("Описание", value=seg.get("description", ""))
        with col2:
            template_labels = {
                "smart_banner_01": "Smart Banner 01 — имиджевый (фото + кэшбек + лого)",
                "smart_banner_02": "Smart Banner 02 — карточка приложения (с городом)",
                "smart_banner_03": "Smart Banner 03 — карточка с рейтингом",
                "smart_banner_04": "Smart Banner 04 — фото на весь экран + город + цена",
                "smart_banner_05": "Smart Banner 05 — фото на весь экран + рейтинг + цена",
                "premium_gold": "Premium Gold — старый шаблон (красная рамка)",
            }
            templates = list(template_labels.keys())
            new_template = st.selectbox(
                "Шаблон креатива",
                templates,
                index=templates.index(seg["template"]) if seg.get("template") in templates else 0,
                format_func=lambda k: template_labels.get(k, k),
            )
            new_limit = st.number_input(
                "Лимит объектов (0 = без лимита)",
                value=int(seg.get("limit", 0)), min_value=0, step=100,
                help="Для тестовых прогонов",
            )
            new_dedupe = st.checkbox(
                "Объединять варианты одного объекта",
                value=bool(seg.get("dedupe_by_url", True)),
                help="В исходном фиде Суточно.ру один отель часто разбит на несколько строк "
                     "(разные комнаты/тарифы). Если включено — оставляем одну строку на отель "
                     "с минимальной ценой. Если выключено — все варианты идут в фид как есть.",
            )

        st.markdown("**Фильтры**")

        # Cities
        city_names = [c["name"] for c in cities]
        city_labels = {c["name"]: f"{c['name']} ({c['count']})" for c in cities}
        current_cities = [c for c in (flt.get("destination_in") or []) if c in city_names]
        new_cities = st.multiselect(
            "Города",
            options=city_names,
            default=current_cities,
            format_func=lambda x: city_labels.get(x, x),
            help="Выбери один или несколько; пусто = все города",
        )

        # Score
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            score_now = float(flt.get("min_score") or 0)
            new_min_score = st.slider("Минимальная оценка", 0.0, 5.0, score_now, step=0.1)
        with col_s2:
            star_values = [s["stars"] for s in stars]
            star_labels = {s["stars"]: f"{s['stars']} ★ ({s['count']})" for s in stars}
            current_stars = [s for s in (flt.get("stars_in") or []) if s in star_values]
            new_stars = st.multiselect(
                "Звёздность",
                options=star_values,
                default=current_stars,
                format_func=lambda x: star_labels.get(x, str(x)),
                help="Пусто = любая звёздность",
            )

        # Price
        col_p1, col_p2 = st.columns(2)
        with col_p1:
            new_min_price = st.number_input(
                "Цена от, ₽/ночь",
                value=int(flt.get("min_price") or 0), min_value=0, step=500,
            )
        with col_p2:
            new_max_price = st.number_input(
                "Цена до, ₽/ночь (0 = без ограничения)",
                value=int(flt.get("max_price") or 0), min_value=0, step=500,
            )

        # Amenities
        amen_names = [a["name"] for a in amenities]
        amen_labels = {a["name"]: f"{a['name']} ({a['count']})" for a in amenities}
        current_amens = [a for a in (flt.get("amenities_any") or []) if a in amen_names]
        new_amens = st.multiselect(
            "Удобства (хотя бы одно из выбранных)",
            options=amen_names,
            default=current_amens,
            format_func=lambda x: amen_labels.get(x, x),
            help="Объект пройдёт, если у него есть ЛЮБОЕ из отмеченных удобств",
        )

        st.markdown("---")
        col_a, col_b, col_c = st.columns([1, 1, 1])
        with col_a:
            save_clicked = st.form_submit_button("💾 Сохранить", type="primary")
        with col_b:
            save_and_run = st.form_submit_button("💾 Сохранить и запустить пересборку")
        with col_c:
            delete_clicked = st.form_submit_button("🗑 Удалить сегмент")

        if save_clicked or save_and_run:
            new_filter: dict[str, Any] = {}
            if new_cities:
                new_filter["destination_in"] = new_cities
            if new_min_score > 0:
                new_filter["min_score"] = round(new_min_score, 2)
            if new_stars:
                new_filter["stars_in"] = sorted(new_stars)
            if new_min_price > 0:
                new_filter["min_price"] = int(new_min_price)
            if new_max_price > 0:
                new_filter["max_price"] = int(new_max_price)
            if new_amens:
                new_filter["amenities_any"] = sorted(new_amens)

            updated = {
                "id": new_id.strip(),
                "description": new_desc.strip(),
                "filter": new_filter,
                "template": new_template,
                "limit": int(new_limit),
                "dedupe_by_url": bool(new_dedupe),
            }
            cfg["segments"][idx] = updated

            try:
                save_segments_file(cfg, sha, f"Update segment {updated['id']}")
                if save_and_run:
                    trigger_workflow()
                    latest_run.clear()
                    st.success("Сохранено и запустил пересборку.")
                else:
                    st.success("Сохранено. Чтобы изменения попали в фид, запусти пересборку сверху.")
                st.rerun()
            except Exception as e:
                st.error(f"Не сохранилось: {e}")

        if delete_clicked:
            removed_id = cfg["segments"][idx]["id"]
            del cfg["segments"][idx]
            try:
                save_segments_file(cfg, sha, f"Delete segment {removed_id}")
                st.success(f"Сегмент {removed_id} удалён.")
                st.rerun()
            except Exception as e:
                st.error(f"Не удалось удалить: {e}")


if __name__ == "__main__":
    main()
