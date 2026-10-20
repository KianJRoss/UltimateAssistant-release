"""Feed API shares the canvas token, renderer store and application lifecycle."""
from fastapi import Depends, HTTPException


def register(app, require_token):
    from . import canvas
    from .settings import settings
    from .canvas_feeds import FeedManager
    manager = FeedManager(settings.user_settings_file.parent / 'canvas-feeds.sqlite3', canvas.store)
    canvas.store.removal_listeners.append(manager.pause_card)

    @app.on_event('startup')
    def start_feeds():
        manager.start()

    @app.on_event('shutdown')
    def stop_feeds():
        manager.stop()

    @app.get('/api/canvas/feeds', dependencies=[Depends(require_token)])
    def list_feeds():
        return {'feeds': manager.list()}

    @app.post('/api/canvas/feeds', dependencies=[Depends(require_token)])
    def save_feed(spec: dict):
        try:
            return manager.save(spec)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete('/api/canvas/feeds/{identity}', dependencies=[Depends(require_token)])
    def remove_feed(identity: str):
        try:
            return manager.remove(identity)
        except KeyError as exc:
            raise HTTPException(404, 'no such feed') from exc

    @app.post('/api/canvas/feeds/{identity}/refresh', dependencies=[Depends(require_token)])
    def refresh_feed(identity: str):
        try:
            manager.get(identity)
            return manager.refresh(identity)
        except KeyError as exc:
            raise HTTPException(404, 'no such feed') from exc

    return manager
