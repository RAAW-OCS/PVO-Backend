"""PVO community APIs.

- Profile survey for any signed-in account (members and guests).
- Public event calendar; Admins and Organizers create, edit, and delete events, including a picture.
- Guest (anonymous) sessions that need no username or password.
- Admin role management (member types, Organizer, Admin).
"""

import base64
import binascii
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import jwt
from flask import Blueprint, current_app, g, jsonify, request
from flask_restful import Api, Resource
from sqlalchemy import inspect, text

from __init__ import db
from api.authorize import token_required
from model.event import Event
from model.profile import ProfileSurvey
from model.user import User


community_api = Blueprint("community_api", __name__, url_prefix="/api")
api = Api(community_api)

# Roles stored in users._role. The website shows them as: Admin (Administrator),
# Organizer (Event organizer), Veteran, Family (Family member), Volunteer,
# User (Member) and Guest.
ADMIN_ROLE = "Admin"
ORGANIZER_ROLE = "Organizer"
GUEST_ROLE = "Guest"
MEMBER_TYPES = ("User", "Veteran", "Family", "Volunteer")      # people may pick these themselves
ASSIGNABLE_ROLES = MEMBER_TYPES + (ORGANIZER_ROLE, ADMIN_ROLE)  # what an Admin can give
EVENT_MANAGER_ROLES = [ADMIN_ROLE, ORGANIZER_ROLE]
# Survey affiliation -> member type it implies
AFFILIATION_MEMBER_TYPES = {
    "Veteran": "Veteran", "Active duty": "Veteran", "Guard or Reserve": "Veteran",
    "Family member": "Family", "Volunteer or supporter": "Volunteer",
}

GUEST_MAX_AGE = timedelta(days=7)
GUEST_UID_PATTERN = re.compile(r"^guest-(\d{8})-[0-9a-f]{6}$")

AFFILIATIONS = (
    "Veteran",
    "Active duty",
    "Guard or Reserve",
    "Family member",
    "Volunteer or supporter",
)

# Base64 inflates uploads by a third and Flask caps requests at 5 MB, so keep
# the decoded image comfortably under that limit.
MAX_EVENT_IMAGE_BYTES = 3_500_000
EVENT_IMAGE_DIR = "events"
UPLOADED_IMAGE_PATTERN = re.compile(r"^/uploads/events/[0-9a-f]{32}\.(jpg|png|gif|webp)$")


def parse_datetime(value, field_name):
    if not value:
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    # Store naive UTC: convert times sent with an offset, keep naive times as they are
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def clean_image_url(value):
    """Allow only http(s) links or images uploaded through EventImageAPI."""
    value = str(value or "").strip()[:1000]
    if not value:
        return None
    if UPLOADED_IMAGE_PATTERN.match(value):
        return value
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value
    raise ValueError("image_url must be an http(s) link or an uploaded event image")


def detect_image_type(raw):
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def remove_uploaded_image(image_url):
    """Delete an uploaded event image from disk; external links are left alone."""
    if not image_url or not UPLOADED_IMAGE_PATTERN.match(image_url):
        return
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], EVENT_IMAGE_DIR, os.path.basename(image_url))
    if os.path.isfile(path):
        os.remove(path)


def set_login_cookie(response, user):
    """Issue the same JWT cookie that /api/authenticate sets."""
    token = jwt.encode({"_uid": user._uid}, current_app.config["SECRET_KEY"], algorithm="HS256")
    if os.environ.get("IS_PRODUCTION", "false").lower() == "true":
        response.set_cookie(
            current_app.config["JWT_TOKEN_NAME"], token, max_age=43200,
            secure=True, httponly=True, path="/", samesite="None", domain=".opencodingsociety.com",
        )
    else:
        response.set_cookie(
            current_app.config["JWT_TOKEN_NAME"], token, max_age=43200,
            secure=False, httponly=False, path="/", samesite="Lax",
        )
    return response


def clear_login_cookies(response):
    """Remove the login cookie from the browser (the opposite of set_login_cookie).

    A browser only removes a cookie when the domain matches the one it was set with, so
    removing just the form this server sets now leaves people signed in when theirs is the
    other form: set before IS_PRODUCTION changed, or by another opencodingsociety.com server.
    Both forms are removed. On localhost the browser ignores the second one.
    """
    name = current_app.config["JWT_TOKEN_NAME"]
    response.delete_cookie(name, path="/", samesite="Lax")
    response.delete_cookie(
        name, path="/", domain=".opencodingsociety.com", secure=True, httponly=True, samesite="None",
    )
    return response


def purge_expired_guests(now=None):
    """Guest accounts are disposable; remove the ones older than GUEST_MAX_AGE."""
    cutoff = ((now or datetime.utcnow()) - GUEST_MAX_AGE).strftime("%Y%m%d")
    removed = 0
    for guest in User.query.filter(User._role == GUEST_ROLE).all():
        match = GUEST_UID_PATTERN.match(guest._uid)
        if match and match.group(1) < cutoff:
            ProfileSurvey.query.filter_by(user_id=guest.id).delete()
            db.session.delete(guest)
            removed += 1
    if removed:
        db.session.commit()
    return removed


# Columns added to profile_surveys after the table first shipped (name, SQL type)
PROFILE_SURVEY_ADDED_COLUMNS = (
    ("affiliation", "VARCHAR(40)"),
    ("era", "VARCHAR(40)"),
    ("meetup", "JSON"),
    ("connect_ok", "BOOLEAN"),
    ("volunteer", "JSON"),
    ("volunteer_availability", "JSON"),
)


def ensure_profile_survey_columns():
    """Add survey columns introduced after the table was first created.

    db.create_all() never alters existing tables, so older databases need the
    new column added explicitly. Safe to run on every start-up.
    """
    inspector = inspect(db.engine)
    if "profile_surveys" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("profile_surveys")}
    missing = [(name, sql_type) for name, sql_type in PROFILE_SURVEY_ADDED_COLUMNS if name not in columns]
    if missing:
        with db.engine.begin() as connection:
            for name, sql_type in missing:
                connection.execute(text(f"ALTER TABLE profile_surveys ADD COLUMN {name} {sql_type}"))


def parse_admin_accounts(setting):
    """Read PVO_ADMINS ("uid:password,uid:password") into [(uid, password)].

    The password is everything after the first ":" so it may contain ":", but not ",".
    Blank or malformed entries are skipped.
    """
    accounts = []
    for entry in (setting or "").split(","):
        uid, separator, password = entry.strip().partition(":")
        uid = uid.strip()
        if uid and separator and password:
            accounts.append((uid, password))
    return accounts


def ensure_admin_accounts(setting=None):
    """Make sure the administrators listed in PVO_ADMINS exist, on every computer and server.

    Accounts live in each backend's own database, which is not in git, so a list in .env
    (also not in git) is how every copy of the backend gets the same administrators.
    Missing accounts are created; existing ones are made Admin and given the password from
    .env, so .env is the source of truth for these accounts. Safe to run on every start-up.
    Returns the uids it checked.
    """
    if setting is None:
        setting = os.environ.get("PVO_ADMINS", "")
    ready = []
    for uid, password in parse_admin_accounts(setting):
        user = User.query.filter_by(_uid=uid).first()
        if user is None:
            user = User(name=uid.title(), uid=uid, password=password, role=ADMIN_ROLE)
            if user.create() is None:
                continue
        else:
            if user.role != ADMIN_ROLE:
                user.role = ADMIN_ROLE
            if not user.is_password(password):
                user.set_password(password)
            db.session.commit()
        ready.append(uid)
    return ready


def admin_user_view(user):
    return {
        "id": user.id,
        "uid": user.uid,
        "name": user.name,
        "email": "" if user.email in (None, "?") else user.email,
        "role": user.role,
    }


class ProfileAPI(Resource):
    @token_required()
    def get(self):
        survey = ProfileSurvey.query.filter_by(user_id=g.current_user.id).first()
        return jsonify(survey.read() if survey else ProfileSurvey.empty())

    @token_required()
    def put(self):
        body = request.get_json(silent=True) or {}
        survey = ProfileSurvey.query.filter_by(user_id=g.current_user.id).first()
        if survey is None:
            survey = ProfileSurvey(user_id=g.current_user.id)
            db.session.add(survey)

        if "affiliation" in body:
            affiliation = str(body["affiliation"] or "").strip()
            if affiliation and affiliation not in AFFILIATIONS:
                db.session.rollback()
                return {"message": f"affiliation must be one of: {', '.join(AFFILIATIONS)}"}, 400
            survey.affiliation = affiliation or None
        if "branch" in body:
            survey.branch = str(body["branch"] or "").strip()[:80] or None
        if "era" in body:
            survey.era = str(body["era"] or "").strip()[:40] or None
        if "connect_ok" in body:
            if not isinstance(body["connect_ok"], bool):
                db.session.rollback()
                return {"message": "connect_ok must be true or false"}, 400
            survey.connect_ok = body["connect_ok"]
        if "service_years" in body:
            years = body["service_years"]
            if years is not None and (isinstance(years, bool) or not isinstance(years, int) or not 0 <= years <= 80):
                db.session.rollback()
                return {"message": "service_years must be a whole number from 0 to 80"}, 400
            survey.service_years = years
        for field in ProfileSurvey.LIST_FIELDS:
            if field in body:
                if not isinstance(body[field], list):
                    db.session.rollback()
                    return {"message": f"{field} must be a list"}, 400
                setattr(survey, field, [str(value)[:100] for value in body[field]][:20])
        if "bio" in body:
            survey.bio = str(body["bio"] or "").strip()[:1000]

        # The affiliation answer sets the member type. Admin, Organizer, Guest (and
        # any other non-member role) are never changed by the survey. This runs after
        # every check above, so a rejected save changes neither the survey nor the role.
        member_type = AFFILIATION_MEMBER_TYPES.get(survey.affiliation) if "affiliation" in body else None
        if member_type and g.current_user.role in MEMBER_TYPES:
            g.current_user.role = member_type
        db.session.commit()
        return jsonify(survey.read())


class EventListAPI(Resource):
    def get(self):
        events = Event.query.order_by(Event.starts_at.asc()).all()
        return jsonify([event.read() for event in events])

    @token_required(roles=EVENT_MANAGER_ROLES)
    def post(self):
        body = request.get_json(silent=True) or {}
        title = str(body.get("title") or "").strip()
        description = str(body.get("description") or "").strip()
        if not title or not description:
            return {"message": "title and description are required"}, 400
        try:
            starts_at = parse_datetime(body.get("starts_at"), "starts_at")
            ends_at = parse_datetime(body["ends_at"], "ends_at") if body.get("ends_at") else None
            image_url = clean_image_url(body.get("image_url"))
        except ValueError as exc:
            return {"message": str(exc)}, 400
        if ends_at and ends_at < starts_at:
            return {"message": "ends_at must be after starts_at"}, 400
        event = Event(
            title=title[:160],
            description=description[:2000],
            starts_at=starts_at,
            ends_at=ends_at,
            location=str(body.get("location") or "").strip()[:255] or None,
            image_url=image_url,
            created_by=g.current_user.id,
        )
        db.session.add(event)
        db.session.commit()
        response = jsonify(event.read())
        response.status_code = 201
        return response


class EventDetailAPI(Resource):
    @token_required(roles=EVENT_MANAGER_ROLES)
    def put(self, event_id):
        event = db.session.get(Event, event_id)
        if event is None:
            return {"message": "Event not found"}, 404
        body = request.get_json(silent=True) or {}
        old_image = event.image_url
        try:
            if "title" in body:
                title = str(body["title"] or "").strip()[:160]
                if not title:
                    raise ValueError("title cannot be empty")
                event.title = title
            if "description" in body:
                description = str(body["description"] or "").strip()[:2000]
                if not description:
                    raise ValueError("description cannot be empty")
                event.description = description
            if "location" in body:
                event.location = str(body["location"] or "").strip()[:255] or None
            if "image_url" in body:
                event.image_url = clean_image_url(body["image_url"])
            if "starts_at" in body:
                event.starts_at = parse_datetime(body["starts_at"], "starts_at")
            if "ends_at" in body:
                event.ends_at = parse_datetime(body["ends_at"], "ends_at") if body["ends_at"] else None
        except ValueError as exc:
            db.session.rollback()
            return {"message": str(exc)}, 400
        if event.ends_at and event.ends_at < event.starts_at:
            db.session.rollback()
            return {"message": "ends_at must be after starts_at"}, 400
        db.session.commit()
        if old_image != event.image_url:
            remove_uploaded_image(old_image)
        return jsonify(event.read())

    @token_required(roles=EVENT_MANAGER_ROLES)
    def delete(self, event_id):
        event = db.session.get(Event, event_id)
        if event is None:
            return {"message": "Event not found"}, 404
        image_url = event.image_url
        db.session.delete(event)
        db.session.commit()
        remove_uploaded_image(image_url)
        return {"message": "Event deleted"}


class EventImageAPI(Resource):
    """Store an event picture sent as base64 (optionally a data: URL)."""

    @token_required(roles=EVENT_MANAGER_ROLES)
    def post(self):
        body = request.get_json(silent=True) or {}
        data = body.get("image")
        if not isinstance(data, str) or not data.strip():
            return {"message": "image is required (base64 or data URL)"}, 400
        if data.startswith("data:"):
            data = data.split(",", 1)[1] if "," in data else ""
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return {"message": "image is not valid base64"}, 400
        if not raw:
            return {"message": "image is empty"}, 400
        if len(raw) > MAX_EVENT_IMAGE_BYTES:
            return {"message": "image must be smaller than 3.5 MB"}, 413
        extension = detect_image_type(raw)
        if extension is None:
            return {"message": "image must be a JPEG, PNG, GIF, or WebP file"}, 400

        folder = os.path.join(current_app.config["UPLOAD_FOLDER"], EVENT_IMAGE_DIR)
        os.makedirs(folder, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.{extension}"
        with open(os.path.join(folder, filename), "wb") as image_file:
            image_file.write(raw)
        return {"image_url": f"/uploads/{EVENT_IMAGE_DIR}/{filename}"}, 201


class GuestSessionAPI(Resource):
    """Sign in as a temporary guest without choosing a username or password."""

    def post(self):
        purge_expired_guests()
        today = datetime.utcnow().strftime("%Y%m%d")
        for _ in range(5):
            uid = f"guest-{today}-{secrets.token_hex(3)}"
            if User.query.filter_by(_uid=uid).first() is None:
                break
        else:
            return {"message": "Could not start a guest session, please try again"}, 503

        guest = User(name="Guest", uid=uid, password=secrets.token_urlsafe(24), role=GUEST_ROLE)
        if guest.create() is None:
            return {"message": "Could not start a guest session, please try again"}, 503

        response = jsonify({
            "message": "Guest session started",
            "user": {"uid": guest.uid, "name": guest.name, "role": guest.role},
        })
        response.status_code = 201
        return set_login_cookie(response, guest)


class AdminUserListAPI(Resource):
    @token_required(roles=ADMIN_ROLE)
    def get(self):
        query = User.query
        if request.args.get("include_guests") not in ("1", "true"):
            query = query.filter(User._role != GUEST_ROLE)
        return jsonify([admin_user_view(user) for user in query.order_by(User.id.asc()).all()])


class AdminUserRoleAPI(Resource):
    @token_required(roles=ADMIN_ROLE)
    def put(self, uid):
        body = request.get_json(silent=True) or {}
        role = body.get("role")
        if role not in ASSIGNABLE_ROLES:
            return {"message": f"role must be one of: {', '.join(ASSIGNABLE_ROLES)}"}, 400
        user = User.query.filter_by(_uid=uid).first()
        if user is None:
            return {"message": f"User {uid} not found"}, 404
        if user.id == g.current_user.id:
            return {"message": "You cannot change your own role. Ask another administrator."}, 400
        if user.role == GUEST_ROLE:
            return {"message": "Guest accounts are temporary and cannot be given a role"}, 400
        user.role = role
        db.session.commit()
        return jsonify(admin_user_view(user))


api.add_resource(ProfileAPI, "/profile/survey")
api.add_resource(EventListAPI, "/events")
api.add_resource(EventDetailAPI, "/events/<int:event_id>")
api.add_resource(EventImageAPI, "/events/image")
api.add_resource(GuestSessionAPI, "/guest/session")
api.add_resource(AdminUserListAPI, "/admin/users")
api.add_resource(AdminUserRoleAPI, "/admin/users/<string:uid>/role")
