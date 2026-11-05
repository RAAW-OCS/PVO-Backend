"""Profile survey data associated with a user."""

from datetime import datetime

from __init__ import db


class ProfileSurvey(db.Model):
    __tablename__ = "profile_surveys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    branch = db.Column(db.String(80), nullable=True)
    service_years = db.Column(db.Integer, nullable=True)
    interests = db.Column(db.JSON, nullable=False, default=list)
    accessibility_needs = db.Column(db.JSON, nullable=False, default=list)
    bio = db.Column(db.String(1000), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("profile_survey", uselist=False))

    def read(self):
        return {
            "branch": self.branch,
            "service_years": self.service_years,
            "interests": self.interests or [],
            "accessibility_needs": self.accessibility_needs or [],
            "bio": self.bio or "",
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
