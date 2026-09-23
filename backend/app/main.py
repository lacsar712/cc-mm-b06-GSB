import json
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Dossier(Base):
    __tablename__ = "dossiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    fragment: Mapped[str] = mapped_column(String(80))
    hit_ids: Mapped[str] = mapped_column(Text)  # JSON 数组，存档的 readings 主键
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class ReadingRenameIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)


class DossierIn(BaseModel):
    fragment: str = Field(min_length=1, max_length=80)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可上报")
    return user


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


def reading_dict(r: Reading) -> dict:
    return {
        "id": r.id,
        "site": r.site,
        "ch4_pct": r.ch4_pct,
        "level": r.level,
        "note": r.note,
        "created_by": r.created_by,
    }


def dossier_summary(d: Dossier) -> dict:
    return {
        "id": d.id,
        "fragment": d.fragment,
        "hit_ids": json.loads(d.hit_ids),
        "created_by": d.created_by,
        "created_at": d.created_at.isoformat(),
    }


def find_matches(db: Session, fragment: str) -> list[Reading]:
    return db.query(Reading).filter(Reading.site.contains(fragment)).order_by(Reading.id.asc()).all()


async def broadcast(payload: dict):
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [reading_dict(r) for r in rows]
    finally:
        db.close()


@app.get("/api/readings/search")
def search_readings(q: str = "", _user: dict = Depends(current_user)):
    fragment = q.strip()
    if not fragment:
        raise HTTPException(status_code=400, detail="请输入测点名片段")
    db = SessionLocal()
    try:
        # 只返回片段命中行；无命中就是空列表，绝不明里暗里回退成全表
        return [reading_dict(r) for r in find_matches(db, fragment)]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {"kind": "create", **reading_dict(row)}
    finally:
        db.close()
    await broadcast(payload)
    return payload


@app.patch("/api/readings/{reading_id}")
async def rename_reading(reading_id: int, body: ReadingRenameIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="测点记录不存在")
        row.site = body.site.strip()
        db.commit()
        db.refresh(row)
        payload = {"kind": "update", **reading_dict(row)}
    finally:
        db.close()
    await broadcast(payload)
    return payload


@app.post("/api/dossiers", status_code=201)
def create_dossier(body: DossierIn, user: dict = Depends(current_user)):
    fragment = body.fragment.strip()
    if not fragment:
        raise HTTPException(status_code=400, detail="请输入测点名片段")
    db = SessionLocal()
    try:
        hits = find_matches(db, fragment)
        if not hits:
            raise HTTPException(status_code=404, detail="无匹配测点")
        dossier = Dossier(
            fragment=fragment,
            hit_ids=json.dumps([r.id for r in hits]),
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(dossier)
        db.commit()
        db.refresh(dossier)
        return dossier_summary(dossier)
    finally:
        db.close()


@app.get("/api/dossiers")
def list_dossiers(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Dossier).order_by(Dossier.id.desc()).all()
        return [dossier_summary(d) for d in rows]
    finally:
        db.close()


@app.get("/api/dossiers/{dossier_id}")
def open_dossier(dossier_id: int, _user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        dossier = db.get(Dossier, dossier_id)
        if dossier is None:
            raise HTTPException(status_code=404, detail="卷宗不存在")
        archived_ids = json.loads(dossier.hit_ids)
        # 此刻按存档时的同一片段重查
        current_rows = find_matches(db, dossier.fragment)
        current_ids = {r.id for r in current_rows}
        archived_rows = {}
        if archived_ids:
            archived_rows = {r.id: r for r in db.query(Reading).filter(Reading.id.in_(archived_ids)).all()}
        archived = []
        for rid in archived_ids:
            row = archived_rows.get(rid)
            still_hit = rid in current_ids
            archived.append(
                {
                    "reading_id": rid,
                    "status": "有效" if still_hit else "失效",
                    "site": row.site if row else None,
                    "ch4_pct": row.ch4_pct if row else None,
                    "level": row.level if row else None,
                }
            )
        return {
            **dossier_summary(dossier),
            "archived": archived,
            "current_ids": [r.id for r in current_rows],
            "current": [reading_dict(r) for r in current_rows],
        }
    finally:
        db.close()


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
