"""Community events displayed on the public PVO calendar."""

from datetime import datetime

from __init__ import db


class Event(db.Model):
    __tablename__ = "events"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.String(2000), nullable=False, default="")
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=True)
    location = db.Column(db.String(255), nullable=True)
    image_url = db.Column(db.String(1000), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    creator = db.relationship("User", backref=db.backref("created_events", lazy=True))

    def read(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "starts_at": self.starts_at.isoformat() + "Z",
            "ends_at": self.ends_at.isoformat() + "Z" if self.ends_at else None,
            "location": self.location or "",
            "image_url": self.image_url or "",
        }
