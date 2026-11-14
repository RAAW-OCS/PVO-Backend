"""Member profile survey: a veteran profile for meeting members with shared interests,
plus an optional volunteer survey."""

from datetime import datetime

from __init__ import db


class ProfileSurvey(db.Model):
    __tablename__ = "profile_surveys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    # Veteran profile
    affiliation = db.Column(db.String(40), nullable=True)
    branch = db.Column(db.String(80), nullable=True)
    era = db.Column(db.String(40), nullable=True)
    service_years = db.Column(db.Integer, nullable=True)
    interests = db.Column(db.JSON, nullable=False, default=list)       # hobbies and shared interests
    meetup = db.Column(db.JSON, nullable=True, default=list)           # how they would like to connect
    connect_ok = db.Column(db.Boolean, nullable=True, default=False)   # OK to be introduced to members
    accessibility_needs = db.Column(db.JSON, nullable=False, default=list)
    bio = db.Column(db.String(1000), nullable=True)

    # Volunteer survey
    volunteer = db.Column(db.JSON, nullable=True, default=list)
    volunteer_availability = db.Column(db.JSON, nullable=True, default=list)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("profile_survey", uselist=False))

    LIST_FIELDS = ("interests", "meetup", "accessibility_needs", "volunteer", "volunteer_availability")

    @staticmethod
    def empty():
        return {
            "affiliation": None,
            "branch": None,
            "era": None,
            "service_years": None,
            "interests": [],
            "meetup": [],
            "connect_ok": False,
            "accessibility_needs": [],
            "bio": "",
            "volunteer": [],
            "volunteer_availability": [],
            "updated_at": None,
        }

    def read(self):
        return {
            "affiliation": self.affiliation,
            "branch": self.branch,
            "era": self.era,
            "service_years": self.service_years,
            "interests": self.interests or [],
            "meetup": self.meetup or [],
            "connect_ok": bool(self.connect_ok),
            "accessibility_needs": self.accessibility_needs or [],
            "bio": self.bio or "",
            "volunteer": self.volunteer or [],
            "volunteer_availability": self.volunteer_availability or [],
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
