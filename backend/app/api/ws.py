from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/simulation")
async def ws_simulation(ws: WebSocket):
    live = ws.app.state.live
    await live.hub.connect("simulation", ws)
    try:
        while True:
            await ws.receive_text()  # keep-alive pings from the client; content ignored
    except WebSocketDisconnect:
        pass
    finally:
        live.hub.disconnect("simulation", ws)


@router.websocket("/ws/bookings/{booking_id}")
async def ws_booking(ws: WebSocket, booking_id: int):
    live = ws.app.state.live
    channel = f"booking:{booking_id}"
    await live.hub.connect(channel, ws)
    try:
        await ws.send_json(live.booking_live(booking_id))  # immediate state on (re)connect
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        live.hub.disconnect(channel, ws)
