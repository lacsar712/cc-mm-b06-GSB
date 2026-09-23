from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, ForeignKey, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

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
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    items: Mapped[list["DossierItem"]] = relationship(
        back_populates="dossier", cascade="all, delete-orphan"
    )


class DossierItem(Base):
    __tablename__ = "dossier_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    dossier_id: Mapped[int] = mapped_column(ForeignKey("dossiers.id"))
    reading_id: Mapped[int] = mapped_column()
    archived_site: Mapped[str] = mapped_column(String(80))
    dossier: Mapped[Dossier] = relationship(back_populates="items")


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class SitePatchIn(BaseModel):
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


def find_by_site(db: Session, fragment: str) -> list[Reading]:
    return (
        db.query(Reading)
        .filter(Reading.site.like(f"%{fragment}%"))
        .order_by(Reading.id.desc())
        .all()
    )


def reading_payload(r: Reading) -> dict:
    return {
        "id": r.id,
        "site": r.site,
        "ch4_pct": r.ch4_pct,
        "level": r.level,
        "note": r.note,
        "created_by": r.created_by,
    }


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


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
def list_readings(site: str | None = None, _user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        if site is None:
            rows = db.query(Reading).order_by(Reading.id.desc()).all()
        else:
            fragment = site.strip()
            if not fragment:
                raise HTTPException(status_code=400, detail="检索片段不能为空")
            rows = find_by_site(db, fragment)
            if not rows:
                # 查不到就是查不到，禁止回退成全表
                raise HTTPException(status_code=404, detail="无匹配测点")
        return [reading_payload(r) for r in rows]
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
        payload = {"id": row.id, "site": row.site, "ch4_pct": row.ch4_pct, "level": row.level, "note": row.note}
    finally:
        db.close()
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)
    return payload


@app.patch("/api/readings/{reading_id}")
async def rename_reading(reading_id: int, body: SitePatchIn, user: dict = Depends(require_writer)):
    new_site = body.site.strip()
    if not new_site:
        raise HTTPException(status_code=400, detail="测点名不能为空")
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="测点不存在")
        row.site = new_site
        db.commit()
        payload = {"type": "reading_updated", "id": row.id, "site": row.site}
    finally:
        db.close()
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)
    return {"id": reading_id, "site": new_site}


@app.post("/api/dossiers", status_code=201)
def create_dossier(body: DossierIn, user: dict = Depends(current_user)):
    fragment = body.fragment.strip()
    if not fragment:
        raise HTTPException(status_code=400, detail="检索片段不能为空")
    db = SessionLocal()
    try:
        hits = find_by_site(db, fragment)
        if not hits:
            raise HTTPException(status_code=404, detail="无匹配测点")
        dossier = Dossier(
            fragment=fragment,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
            items=[DossierItem(reading_id=r.id, archived_site=r.site) for r in hits],
        )
        db.add(dossier)
        db.commit()
        db.refresh(dossier)
        return dossier_payload(db, dossier)
    finally:
        db.close()


@app.get("/api/dossiers")
def list_dossiers(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Dossier).order_by(Dossier.id.desc()).all()
        return [
            {
                "id": d.id,
                "fragment": d.fragment,
                "created_by": d.created_by,
                "created_at": d.created_at.isoformat(),
                "item_count": len(d.items),
            }
            for d in rows
        ]
    finally:
        db.close()


@app.get("/api/dossiers/{dossier_id}")
def get_dossier(dossier_id: int, _user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        dossier = db.get(Dossier, dossier_id)
        if dossier is None:
            raise HTTPException(status_code=404, detail="卷宗不存在")
        return dossier_payload(db, dossier)
    finally:
        db.close()


def dossier_payload(db: Session, dossier: Dossier) -> dict:
    items = []
    for item in dossier.items:
        row = db.get(Reading, item.reading_id)
        # 此刻重查：行已删除，或测点名被改正后不再包含原片段 → 存档主键失效
        stale = row is None or dossier.fragment not in row.site
        items.append(
            {
                "reading_id": item.reading_id,
                "archived_site": item.archived_site,
                "stale": stale,
                "current": reading_payload(row) if row is not None else None,
            }
        )
    return {
        "id": dossier.id,
        "fragment": dossier.fragment,
        "created_by": dossier.created_by,
        "created_at": dossier.created_at.isoformat(),
        "items": items,
    }


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
