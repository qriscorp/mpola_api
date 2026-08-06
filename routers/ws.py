"""
WebSocket endpoint for live notification/wallet-balance push.

Auth happens via a `token` query param (browsers/React Native can't set
Authorization headers on a WebSocket handshake) — same access-token JWT
used everywhere else, verified the same way.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from database import SessionLocal
from database.tables import User
from repository.auth_repo import AuthRepo
from realtime import manager

router = APIRouter(tags=["Realtime"])


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    if not token:
        await websocket.close(code=4001)
        return

    try:
        auth_user = AuthRepo.verify_token(token)
    except Exception:
        await websocket.close(code=4001)
        return

    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.username == auth_user.username).first()
        if not user or not user.is_active:
            await websocket.close(code=4001)
            return
        user_id = user.id
    finally:
        db.close()

    await manager.connect(user_id, websocket)
    try:
        while True:
            # Clients don't need to send anything — this just waits for
            # disconnect. A stray message is ignored (no ping/pong protocol yet).
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(user_id, websocket)
