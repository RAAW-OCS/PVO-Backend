"""Authenticated profile surveys and public/admin community events."""

from datetime import datetime

from flask import Blueprint, g, jsonify, request
from flask_restful import Api, Resource

from __init__ import db
from api.authorize import token_required
from model.event import Event
from model.profile import ProfileSurvey


community_api = Blueprint("community_api", __name__, url_prefix="/api")
api = Api(community_api)


def parse_datetime(value, field_name):
    if not value:
        raise ValueError(f"{field_name} is required")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc


class ProfileAPI(Resource):
    @token_required()
    def get(self):
        survey = ProfileSurvey.query.filter_by(user_id=g.current_user.id).first()
        return jsonify(survey.read() if survey else {
            "branch": None,
            "service_years": None,
            "interests": [],
            "accessibility_needs": [],
            "bio": "",
        })

    @token_required()
    def put(self):
        body = request.get_json() or {}
        survey = ProfileSurvey.query.filter_by(user_id=g.current_user.id).first()
        if survey is None:
            survey = ProfileSurvey(user_id=g.current_user.id)
            db.session.add(survey)

        if "branch" in body:
            survey.branch = str(body["branch"]).strip()[:80] or None
        if "service_years" in body:
            if body["service_years"] is not None and (
                not isinstance(body["service_years"], int) or body["service_years"] < 0
            ):
                return {"message": "service_years must be a non-negative integer"}, 400
            survey.service_years = body["service_years"]
        for field in ("interests", "accessibility_needs"):
            if field in body:
                if not isinstance(body[field], list):
                    return {"message": f"{field} must be a list"}, 400
                setattr(survey, field, [str(value)[:100] for value in body[field]][:20])
        if "bio" in body:
            survey.bio = str(body["bio"]).strip()[:1000]
        db.session.commit()
        return jsonify(survey.read())


class EventListAPI(Resource):
    def get(self):
        events = Event.query.order_by(Event.starts_at.asc()).all()
        return jsonify([event.read() for event in events])

    @token_required(roles="Admin")
    def post(self):
        body = request.get_json() or {}
        title = str(body.get("title", "")).strip()
        description = str(body.get("description", "")).strip()
        if not title or not description:
            return {"message": "title and description are required"}, 400
        try:
            starts_at = parse_datetime(body.get("starts_at"), "starts_at")
            ends_at = parse_datetime(body["ends_at"], "ends_at") if body.get("ends_at") else None
        except ValueError as exc:
            return {"message": str(exc)}, 400
        if ends_at and ends_at < starts_at:
            return {"message": "ends_at must be after starts_at"}, 400
        event = Event(
            title=title[:160],
            description=description[:2000],
            starts_at=starts_at,
            ends_at=ends_at,
            location=str(body.get("location", "")).strip()[:255] or None,
            image_url=str(body.get("image_url", "")).strip()[:1000] or None,
            created_by=g.current_user.id,
        )
        db.session.add(event)
        db.session.commit()
        return jsonify(event.read()), 201


class EventDetailAPI(Resource):
    @token_required(roles="Admin")
    def put(self, event_id):
        event = Event.query.get(event_id)
        if event is None:
            return {"message": "Event not found"}, 404
        body = request.get_json() or {}
        if "title" in body:
            event.title = str(body["title"]).strip()[:160]
        if "description" in body:
            event.description = str(body["description"]).strip()[:2000]
        if "location" in body:
            event.location = str(body["location"]).strip()[:255] or None
        if "image_url" in body:
            event.image_url = str(body["image_url"]).strip()[:1000] or None
        for field in ("starts_at", "ends_at"):
            if field in body:
                try:
                    value = parse_datetime(body[field], field) if body[field] else None
                except ValueError as exc:
                    return {"message": str(exc)}, 400
                setattr(event, field, value)
        if event.ends_at and event.ends_at < event.starts_at:
            return {"message": "ends_at must be after starts_at"}, 400
        db.session.commit()
        return jsonify(event.read())

    @token_required(roles="Admin")
    def delete(self, event_id):
        event = Event.query.get(event_id)
        if event is None:
            return {"message": "Event not found"}, 404
        db.session.delete(event)
        db.session.commit()
        return {"message": "Event deleted"}


api.add_resource(ProfileAPI, "/profile/survey")
api.add_resource(EventListAPI, "/events")
api.add_resource(EventDetailAPI, "/events/<int:event_id>")
