from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text, BigInteger
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from py.utilities.setting import Config

Base = declarative_base()

class ImageAsset(Base):
    __tablename__ = 'image_assets'

    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String(50), index=True, nullable=False)
    parent_sku = Column(String(50), index=True)
    version = Column(Integer, default=1)
    filename = Column(String(255), nullable=False)
    path = Column(String(500), nullable=False)
    source = Column(String(50), nullable=False)  # catalog, big_picture, dropbox
    sha256 = Column(String(64), unique=True, nullable=False)
    phash = Column(String(64), index=True)
    file_size = Column(Integer)
    width = Column(Integer)
    height = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    is_canonical = Column(Boolean, default=False)
    deleted_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<ImageAsset(id={self.id}, sku={self.sku}, filename={self.filename})>"

# Create engine and session. MySQL may close idle connections while they are
# still present in SQLAlchemy's pool; validate pooled connections before use
# and periodically replace them so requests do not require an app restart.
engine = create_engine(
    Config.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_tables():
    Base.metadata.create_all(bind=engine)