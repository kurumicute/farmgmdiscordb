"""啟動介紹網站：python -m app.site（預設 http://127.0.0.1:24444；對外公開請設定 SITE_HOST=0.0.0.0）。"""

import uvicorn

from app.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.site.server import create_site_app


def main() -> None:
    settings = get_settings()
    # 介紹網站只做唯讀的彙總查詢，連線池用小一點即可
    engine = make_engine(settings.database_url, pool_size=2, max_overflow=2, pool_timeout=5)
    app = create_site_app(
        make_session_factory(engine),
        application_id=settings.discord_application_id,
        support_url=settings.site_support_url,
        music_enabled=settings.music_enabled,
        music_application_id=settings.music_application_id if settings.music_bot_token else None,
        music_prefix_commands=settings.music_prefix_commands,
        music_prefix=settings.music_prefix,
        music_playlist_limit=settings.music_playlist_limit,
        music_idle_timeout=settings.music_idle_timeout,
        music_volume=settings.music_volume,
    )
    print(f"介紹網站：http://{settings.site_host}:{settings.site_port}")
    try:
        uvicorn.run(app, host=settings.site_host, port=settings.site_port)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
