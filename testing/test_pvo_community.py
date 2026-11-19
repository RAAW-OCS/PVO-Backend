"""Tests for the PVO community APIs (events, pictures, survey, guests, roles, sign-up, login).

Run from the backend folder:

    venv/bin/python testing/test_pvo_community.py

The tests use a throwaway SQLite database and upload folder, so they never
touch instance/volumes/user_management.db or instance/uploads.
"""

import base64
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP_DIR = tempfile.mkdtemp(prefix="pvo-tests-")

os.environ["PVO_TEST_DATABASE_URI"] = "sqlite:///" + os.path.join(TEMP_DIR, "test.db")
os.environ["PVO_DB_PATH"] = os.path.join(TEMP_DIR, "pvo.db")
os.environ["IS_PRODUCTION"] = "false"
os.environ.setdefault("SECRET_KEY", "test-secret")
sys.path.insert(0, BACKEND_ROOT)
os.chdir(BACKEND_ROOT)

from main import app  # noqa: E402  (env must be set before the app is imported)
from __init__ import db  # noqa: E402
from model.user import User  # noqa: E402
from model.profile import ProfileSurvey  # noqa: E402
from api.community_api import (  # noqa: E402
    ensure_admin_accounts, ensure_profile_survey_columns, parse_admin_accounts, purge_expired_guests,
)
from sqlalchemy import inspect, text  # noqa: E402

# 1x1 transparent PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
PASSWORD = "Test-pass-123"


def make_user(uid, role="User", name=None):
    with app.app_context():
        existing = User.query.filter_by(_uid=uid).first()
        if existing:
            return
        User(name=name or uid.title(), uid=uid, password=PASSWORD, role=role).create()


def signed_in_client(uid):
    client = app.test_client()
    response = client.post("/api/authenticate", json={"uid": uid, "password": PASSWORD})
    assert response.status_code == 200, response.get_data(as_text=True)
    return client


def guest_client():
    client = app.test_client()
    response = client.post("/api/guest/session")
    assert response.status_code == 201, response.get_data(as_text=True)
    return client, response.get_json()["user"]


def role_in_db(uid):
    with app.app_context():
        user = User.query.filter_by(_uid=uid).first()
        return user.role if user else None


def png_base64():
    return base64.b64encode(PNG_BYTES).decode()


SAMPLE_EVENT = {"title": "Coffee", "description": "Meet up", "starts_at": "2026-12-01T09:00"}


class CommunityApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["UPLOAD_FOLDER"] = os.path.join(TEMP_DIR, "uploads")
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        with app.app_context():
            db.create_all()
        make_user("admin_tester", role="Admin", name="Admin Tester")
        make_user("member_tester", name="Member Tester")

    # ---- events -----------------------------------------------------------------

    def test_events_are_public_but_members_and_guests_cannot_post(self):
        anonymous = app.test_client()
        self.assertEqual(anonymous.get("/api/events").status_code, 200)

        event = {"title": "Coffee", "description": "Meet up", "starts_at": "2026-12-01T09:00"}
        self.assertEqual(anonymous.post("/api/events", json=event).status_code, 401)
        self.assertEqual(signed_in_client("member_tester").post("/api/events", json=event).status_code, 403)
        guest, _ = guest_client()
        self.assertEqual(guest.post("/api/events", json=event).status_code, 403)

    def test_admin_posts_event_with_uploaded_picture_text_and_time(self):
        admin = signed_in_client("admin_tester")
        upload = admin.post("/api/events/image", json={
            "image": "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode(),
        })
        self.assertEqual(upload.status_code, 201, upload.get_data(as_text=True))
        image_url = upload.get_json()["image_url"]
        self.assertRegex(image_url, r"^/uploads/events/[0-9a-f]{32}\.png$")
        image_path = os.path.join(app.config["UPLOAD_FOLDER"], "events", os.path.basename(image_url))
        self.assertTrue(os.path.isfile(image_path))

        # The picture is served back by the existing /uploads route
        served = app.test_client().get(image_url)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.data, PNG_BYTES)

        created = admin.post("/api/events", json={
            "title": "Veterans Day breakfast",
            "description": "Pancakes at the community park.",
            "starts_at": "2026-11-20T08:00:00",
            "ends_at": "2026-11-20T10:30:00",
            "location": "Poway Community Park",
            "image_url": image_url,
        })
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        event = created.get_json()
        self.assertEqual(event["image_url"], image_url)
        self.assertEqual(event["starts_at"], "2026-11-20T08:00:00Z")
        self.assertEqual(event["ends_at"], "2026-11-20T10:30:00Z")

        listed = app.test_client().get("/api/events").get_json()
        self.assertIn(event["id"], [item["id"] for item in listed])

        updated = admin.put(f"/api/events/{event['id']}", json={"title": "Veterans Day pancake breakfast"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["title"], "Veterans Day pancake breakfast")

        deleted = admin.delete(f"/api/events/{event['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(os.path.exists(image_path), "uploaded picture should be removed with its event")

    def test_replacing_an_event_picture_removes_the_old_file(self):
        admin = signed_in_client("admin_tester")
        first = admin.post("/api/events/image", json={"image": base64.b64encode(PNG_BYTES).decode()}).get_json()["image_url"]
        second = admin.post("/api/events/image", json={"image": base64.b64encode(PNG_BYTES).decode()}).get_json()["image_url"]
        event = admin.post("/api/events", json={
            "title": "Flag retirement", "description": "Ceremony", "starts_at": "2026-12-05T10:00", "image_url": first,
        }).get_json()
        admin.put(f"/api/events/{event['id']}", json={"image_url": second})
        folder = os.path.join(app.config["UPLOAD_FOLDER"], "events")
        self.assertFalse(os.path.exists(os.path.join(folder, os.path.basename(first))))
        self.assertTrue(os.path.exists(os.path.join(folder, os.path.basename(second))))
        admin.delete(f"/api/events/{event['id']}")

    def test_event_validation(self):
        admin = signed_in_client("admin_tester")
        base = {"title": "Picnic", "description": "Bring a dish", "starts_at": "2026-12-01T12:00"}
        cases = [
            ({**base, "title": ""}, "title and description are required"),
            ({**base, "title": None}, "title and description are required"),
            ({**base, "description": "   "}, "title and description are required"),
            ({**base, "starts_at": None}, "starts_at is required"),
            ({**base, "starts_at": "next tuesday"}, "starts_at must be an ISO-8601 datetime"),
            ({**base, "ends_at": "2026-12-01T11:00"}, "ends_at must be after starts_at"),
            ({**base, "image_url": "javascript:alert(1)"}, "image_url must be an http(s) link"),
        ]
        for body, message in cases:
            with self.subTest(message=message):
                response = admin.post("/api/events", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.get_json()["message"])

        external = admin.post("/api/events", json={**base, "image_url": "https://example.org/photo.jpg"})
        self.assertEqual(external.status_code, 201)
        event_id = external.get_json()["id"]
        for body in ({"title": ""}, {"title": None}, {"description": ""}, {"starts_at": None},
                     {"ends_at": "2026-11-30T09:00"}, {"image_url": "/uploads/../secret.db"}):
            with self.subTest(update=body):
                self.assertEqual(admin.put(f"/api/events/{event_id}", json=body).status_code, 400)
        stored = [e for e in admin.get("/api/events").get_json() if e["id"] == event_id][0]
        self.assertEqual(stored["title"], "Picnic", "rejected edits must not be saved")
        admin.delete(f"/api/events/{event_id}")
        self.assertEqual(admin.put("/api/events/999999", json={"title": "x"}).status_code, 404)
        self.assertEqual(admin.delete("/api/events/999999").status_code, 404)

    def test_event_times_are_stored_as_utc(self):
        admin = signed_in_client("admin_tester")
        offset = admin.post("/api/events", json={**SAMPLE_EVENT, "starts_at": "2026-12-01T09:00:00-08:00"}).get_json()
        zulu = admin.post("/api/events", json={**SAMPLE_EVENT, "starts_at": "2026-12-01T17:00:00Z"}).get_json()
        self.assertEqual(offset["starts_at"], "2026-12-01T17:00:00Z")
        self.assertEqual(zulu["starts_at"], "2026-12-01T17:00:00Z")
        self.assertIsNone(zulu["ends_at"])
        self.assertEqual(zulu["location"], "")
        self.assertEqual(zulu["image_url"], "")
        listed = app.test_client().get("/api/events").get_json()
        self.assertEqual([e["starts_at"] for e in listed], sorted(e["starts_at"] for e in listed))
        for event in (offset, zulu):
            admin.delete(f"/api/events/{event['id']}")

    def test_event_picture_upload_rejects_bad_files(self):
        admin = signed_in_client("admin_tester")
        self.assertEqual(admin.post("/api/events/image", json={}).status_code, 400)
        self.assertEqual(admin.post("/api/events/image", json={"image": "not base64!!"}).status_code, 400)
        text_file = base64.b64encode(b"<script>alert(1)</script>").decode()
        self.assertEqual(admin.post("/api/events/image", json={"image": text_file}).status_code, 400)
        too_big = base64.b64encode(PNG_BYTES[:8] + b"\0" * 3_500_000).decode()
        oversized = admin.post("/api/events/image", json={"image": too_big})
        self.assertEqual(oversized.status_code, 413, oversized.get_data(as_text=True))
        member = signed_in_client("member_tester")
        self.assertEqual(member.post("/api/events/image", json={"image": base64.b64encode(PNG_BYTES).decode()}).status_code, 403)

    # ---- guests -----------------------------------------------------------------

    def test_guest_session_signs_in_without_credentials(self):
        client, user = guest_client()
        self.assertEqual(user["role"], "Guest")
        self.assertRegex(user["uid"], r"^guest-\d{8}-[0-9a-f]{6}$")
        me = client.get("/api/id")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.get_json()["role"], "Guest")
        self.assertNotIn("password", me.get_json())

        logout = client.delete("/api/authenticate")
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(client.get("/api/id").status_code, 401)

    def test_expired_guests_are_purged_with_their_survey(self):
        old_uid = "guest-" + (datetime.utcnow() - timedelta(days=10)).strftime("%Y%m%d") + "-abc123"
        with app.app_context():
            old_guest = User(name="Guest", uid=old_uid, password=PASSWORD, role="Guest").create()
            db.session.add(ProfileSurvey(user_id=old_guest.id, interests=[], accessibility_needs=[]))
            db.session.commit()
            old_id = old_guest.id
        _, recent = guest_client()
        with app.app_context():
            purge_expired_guests()
            self.assertIsNone(User.query.filter_by(_uid=old_uid).first())
            self.assertIsNone(ProfileSurvey.query.filter_by(user_id=old_id).first())
            self.assertIsNotNone(User.query.filter_by(_uid=recent["uid"]).first())

    # ---- profile survey ---------------------------------------------------------

    def test_profile_survey_round_trip_and_validation(self):
        client, _ = guest_client()
        self.assertEqual(app.test_client().get("/api/profile/survey").status_code, 401)
        empty = client.get("/api/profile/survey").get_json()
        self.assertIsNone(empty["affiliation"])

        saved = client.put("/api/profile/survey", json={
            "affiliation": "Veteran",
            "branch": "Navy",
            "service_years": 6,
            "interests": ["Volunteering", "Community events"],
            "accessibility_needs": ["Transportation"],
            "bio": "Retired corpsman.",
        })
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        survey = client.get("/api/profile/survey").get_json()
        self.assertEqual(survey["affiliation"], "Veteran")
        self.assertEqual(survey["branch"], "Navy")
        self.assertEqual(survey["service_years"], 6)
        self.assertEqual(survey["interests"], ["Volunteering", "Community events"])
        self.assertEqual(survey["bio"], "Retired corpsman.")

        for body in ({"affiliation": "Pirate"}, {"service_years": -1}, {"service_years": "six"}, {"interests": "x"}):
            with self.subTest(body=body):
                self.assertEqual(client.put("/api/profile/survey", json=body).status_code, 400)
        # a rejected update must not wipe what was saved
        self.assertEqual(client.get("/api/profile/survey").get_json()["branch"], "Navy")

    def test_survey_columns_added_after_launch_are_created_on_startup(self):
        added = ["affiliation", "era", "meetup", "connect_ok", "volunteer", "volunteer_availability"]
        make_user("migration_tester")
        signed_in_client("migration_tester").put("/api/profile/survey", json={"branch": "Coast Guard", "bio": "Kept"})
        with app.app_context():
            with db.engine.begin() as connection:
                for column in added:
                    connection.execute(text(f"ALTER TABLE profile_surveys DROP COLUMN {column}"))
            columns = [c["name"] for c in inspect(db.engine).get_columns("profile_surveys")]
            self.assertFalse(any(column in columns for column in added))
            ensure_profile_survey_columns()
            ensure_profile_survey_columns()  # idempotent
            columns = [c["name"] for c in inspect(db.engine).get_columns("profile_surveys")]
            for column in added:
                self.assertIn(column, columns)
        # existing answers survive, and the new columns read back as empty defaults
        survey = signed_in_client("migration_tester").get("/api/profile/survey").get_json()
        self.assertEqual(survey["branch"], "Coast Guard")
        self.assertEqual(survey["bio"], "Kept")
        self.assertIsNone(survey["affiliation"])
        self.assertEqual(survey["meetup"], [])
        self.assertFalse(survey["connect_ok"])

    def test_veteran_profile_and_volunteer_survey_fields(self):
        client, _ = guest_client()
        profile = {
            "affiliation": "Veteran", "branch": "Army", "era": "Gulf War", "service_years": 8,
            "interests": ["Fishing", "Woodworking"], "meetup": ["Coffee meetups"], "connect_ok": True,
            "bio": "Retired mechanic.",
        }
        self.assertEqual(client.put("/api/profile/survey", json=profile).status_code, 200)
        volunteer = {"volunteer": ["Home repairs", "Driving to appointments"], "volunteer_availability": ["Weekends"]}
        self.assertEqual(client.put("/api/profile/survey", json=volunteer).status_code, 200)
        saved = client.get("/api/profile/survey").get_json()
        for key, value in {**profile, **volunteer}.items():
            self.assertEqual(saved[key], value, key)
        # saving one section leaves the other alone
        client.put("/api/profile/survey", json={"connect_ok": False})
        saved = client.get("/api/profile/survey").get_json()
        self.assertFalse(saved["connect_ok"])
        self.assertEqual(saved["volunteer"], volunteer["volunteer"])
        self.assertEqual(client.put("/api/profile/survey", json={"connect_ok": "yes"}).status_code, 400)
        self.assertEqual(client.put("/api/profile/survey", json={"meetup": "coffee"}).status_code, 400)

    # ---- roles ------------------------------------------------------------------

    def test_admin_manages_roles(self):
        make_user("promote_me")
        guest_client()
        admin = signed_in_client("admin_tester")

        users = admin.get("/api/admin/users").get_json()
        roles = {user["uid"]: user["role"] for user in users}
        self.assertEqual(roles["promote_me"], "User")
        self.assertFalse(any(role == "Guest" for role in roles.values()), "guests hidden by default")
        with_guests = admin.get("/api/admin/users?include_guests=1").get_json()
        self.assertTrue(any(user["role"] == "Guest" for user in with_guests))
        self.assertFalse(any("password" in user for user in users))

        promoted = admin.put("/api/admin/users/promote_me/role", json={"role": "Admin"})
        self.assertEqual(promoted.status_code, 200)
        self.assertEqual(promoted.get_json()["role"], "Admin")
        new_admin = signed_in_client("promote_me")
        event = new_admin.post("/api/events", json={"title": "T", "description": "D", "starts_at": "2026-12-09T09:00"})
        self.assertEqual(event.status_code, 201)
        new_admin.delete(f"/api/events/{event.get_json()['id']}")

        self.assertEqual(admin.put("/api/admin/users/promote_me/role", json={"role": "User"}).status_code, 200)
        own = admin.put("/api/admin/users/admin_tester/role", json={"role": "User"})
        self.assertEqual(own.status_code, 400)
        self.assertEqual(own.get_json()["message"], "You cannot change your own role. Ask another administrator.")
        self.assertEqual(role_in_db("admin_tester"), "Admin")
        self.assertEqual(admin.put("/api/admin/users/promote_me/role", json={"role": "Owner"}).status_code, 400)
        self.assertEqual(admin.put("/api/admin/users/nobody_here/role", json={"role": "User"}).status_code, 404)

    def test_non_admins_cannot_manage_roles(self):
        member = signed_in_client("member_tester")
        self.assertEqual(member.get("/api/admin/users").status_code, 403)
        self.assertEqual(member.put("/api/admin/users/member_tester/role", json={"role": "Admin"}).status_code, 403)
        # Updating your own profile must not be a way to become an admin
        member.put("/api/user", json={"role": "Admin"})
        self.assertEqual(member.get("/api/id").get_json()["role"], "User")

    def test_guest_role_cannot_be_changed(self):
        _, guest = guest_client()
        admin = signed_in_client("admin_tester")
        response = admin.put(f"/api/admin/users/{guest['uid']}/role", json={"role": "Admin"})
        self.assertEqual(response.status_code, 400)

    # ---- admin accounts from .env (PVO_ADMINS) -------------------------------------

    def test_admin_accounts_setting_is_parsed_safely(self):
        self.assertEqual(parse_admin_accounts(" alpha:Pass-one-1 , beta:Pass-two-2 "), [("alpha", "Pass-one-1"), ("beta", "Pass-two-2")])
        self.assertEqual(parse_admin_accounts("colon:pa:ss"), [("colon", "pa:ss")])  # first ":" splits
        self.assertEqual(parse_admin_accounts(""), [])
        self.assertEqual(parse_admin_accounts(None), [])
        self.assertEqual(parse_admin_accounts("nopassword,:nouid,blank:,,"), [])

    def test_admins_listed_in_env_are_created_and_can_log_in(self):
        with app.app_context():
            ready = ensure_admin_accounts("envadmin_one:First-pass-1,envadmin_two:Second:pass-2")
        self.assertEqual(ready, ["envadmin_one", "envadmin_two"])
        for uid, password in (("envadmin_one", "First-pass-1"), ("envadmin_two", "Second:pass-2")):
            client = app.test_client()
            self.assertEqual(client.post("/api/authenticate", json={"uid": uid, "password": password}).status_code, 200)
            self.assertEqual(client.get("/api/id").get_json()["role"], "Admin")
            self.assertEqual(client.get("/api/admin/users").status_code, 200)
        # running again (every start-up) does not duplicate anything
        with app.app_context():
            ensure_admin_accounts("envadmin_one:First-pass-1")
            self.assertEqual(User.query.filter_by(_uid="envadmin_one").count(), 1)

    def test_existing_account_listed_in_env_is_promoted_and_uses_the_env_password(self):
        make_user("env_existing")  # a Member with PASSWORD
        with app.app_context():
            ensure_admin_accounts("env_existing:New-env-pass-1")
        self.assertEqual(role_in_db("env_existing"), "Admin")
        self.assertEqual(self.login_status("env_existing", "New-env-pass-1"), 200)
        self.assertEqual(self.login_status("env_existing", PASSWORD), 401)

    def test_no_admin_setting_changes_nothing(self):
        with app.app_context():
            before = User.query.count()
            self.assertEqual(ensure_admin_accounts(""), [])
            self.assertEqual(User.query.count(), before)

    # ---- account privacy --------------------------------------------------------

    def test_account_responses_never_include_password_hashes(self):
        signup = app.test_client().post("/api/user/guest", json={
            "name": "New Member", "uid": "new_member", "password": PASSWORD,
        })
        self.assertIn(signup.status_code, (200, 409))
        self.assertNotIn("password", signup.get_json())
        member = signed_in_client("member_tester")
        self.assertNotIn("password", member.get("/api/id").get_json())
        for user in member.get("/api/user").get_json():
            self.assertNotIn("password", user)
        updated = member.put("/api/user", json={"name": "Member Tester"})
        self.assertEqual(updated.status_code, 200)
        self.assertNotIn("password", updated.get_json())
        admin = signed_in_client("admin_tester")
        for user in admin.get("/api/user?page=1").get_json()["users"]:
            self.assertNotIn("password", user)
        with mock.patch("api.user.GitHubUser") as github:
            github.return_value.get.return_value = ({}, 200)
            created = app.test_client().post("/api/user", json={
                "name": "Github Member", "uid": "github_member", "password": PASSWORD,
            })
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        self.assertNotIn("password", created.get_json())

    # ---- logging in ----------------------------------------------------------------

    def login_status(self, uid, password=PASSWORD):
        return app.test_client().post("/api/authenticate", json={"uid": uid, "password": password}).status_code

    def test_login_accepts_any_capitalisation_and_email(self):
        make_user("CaseUser")
        with app.app_context():
            user = User.query.filter_by(_uid="CaseUser").first()
            user.email = "case.user@example.org"
            db.session.commit()
        self.assertEqual(self.login_status("CaseUser"), 200)
        self.assertEqual(self.login_status("caseuser"), 200)
        self.assertEqual(self.login_status("  CASEUSER "), 200)
        self.assertEqual(self.login_status("Case.User@Example.org"), 200)
        self.assertEqual(self.login_status("caseuser", "wrong-password"), 401)
        self.assertEqual(self.login_status("Case User"), 401)  # a display name is not a username
        client = app.test_client()
        client.post("/api/authenticate", json={"uid": "caseuser", "password": PASSWORD})
        self.assertEqual(client.get("/api/id").get_json()["uid"], "CaseUser")

    def test_ambiguous_logins_are_refused(self):
        make_user("Twin")
        make_user("twin")
        self.assertEqual(self.login_status("Twin"), 200)   # exact match still works
        self.assertEqual(self.login_status("twin"), 200)
        self.assertEqual(self.login_status("TWIN"), 401)   # two accounts match: refuse to guess
        make_user("shared_one")
        make_user("shared_two")
        with app.app_context():
            for uid in ("shared_one", "shared_two"):
                User.query.filter_by(_uid=uid).first().email = "shared@example.org"
            db.session.commit()
        self.assertEqual(self.login_status("shared@example.org"), 401)

    def test_signup_with_taken_username_does_not_leak_that_account(self):
        response = app.test_client().post("/api/user/guest", json={
            "name": "Impostor", "uid": "admin_tester", "password": "whatever-123",
        })
        self.assertEqual(response.status_code, 409)
        body = response.get_json()
        self.assertEqual(body, {"message": "The username admin_tester is already taken"})

        # The GitHub-checked sign-up (POST /api/user) follows the same rule
        with mock.patch("api.user.GitHubUser") as github:
            github.return_value.get.return_value = ({}, 200)
            response = app.test_client().post("/api/user", json={
                "name": "Impostor", "uid": "admin_tester", "password": "whatever-123",
            })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(set(response.get_json()), {"message"})
        self.assertIn("already taken", response.get_json()["message"])
        self.assertEqual(role_in_db("admin_tester"), "Admin")

    # ---- roles: who may do what ---------------------------------------------------

    def test_admin_can_give_every_assignable_role(self):
        make_user("role_target", name="Role Target")
        admin = signed_in_client("admin_tester")
        target = signed_in_client("role_target")
        for role in ("User", "Veteran", "Family", "Volunteer", "Organizer", "Admin"):
            with self.subTest(role=role):
                response = admin.put("/api/admin/users/role_target/role", json={"role": role})
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                body = response.get_json()
                self.assertEqual(set(body), {"id", "uid", "name", "email", "role"})
                self.assertEqual(body["role"], role)
                self.assertEqual(body["email"], "")
                self.assertEqual(target.get("/api/id").get_json()["role"], role)
        for role in ("Guest", "Teacher", "admin", "", None):
            with self.subTest(invalid_role=role):
                response = admin.put("/api/admin/users/role_target/role", json={"role": role})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.get_json()["message"].startswith("role must be one of: "))
        self.assertEqual(admin.put("/api/admin/users/role_target/role").status_code, 400)  # no body
        self.assertEqual(role_in_db("role_target"), "Admin", "rejected requests must not change the role")
        # The newly made Admin can now manage roles too
        make_user("role_target_two")
        self.assertEqual(target.put("/api/admin/users/role_target_two/role", json={"role": "Volunteer"}).status_code, 200)
        self.assertEqual(role_in_db("role_target_two"), "Volunteer")

    def test_organizer_manages_events_but_not_people(self):
        make_user("organizer_tester", role="Organizer", name="Organizer Tester")
        organizer = signed_in_client("organizer_tester")
        self.assertEqual(organizer.get("/api/id").get_json()["role"], "Organizer")

        upload = organizer.post("/api/events/image", json={"image": png_base64()})
        self.assertEqual(upload.status_code, 201, upload.get_data(as_text=True))
        image_url = upload.get_json()["image_url"]
        created = organizer.post("/api/events", json={**SAMPLE_EVENT, "image_url": image_url})
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        event_id = created.get_json()["id"]
        edited = organizer.put(f"/api/events/{event_id}", json={"location": "VFW Hall"})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.get_json()["location"], "VFW Hall")
        self.assertEqual(organizer.delete(f"/api/events/{event_id}").status_code, 200)
        image_path = os.path.join(app.config["UPLOAD_FOLDER"], "events", os.path.basename(image_url))
        self.assertFalse(os.path.exists(image_path))

        self.assertEqual(organizer.get("/api/admin/users").status_code, 403)
        self.assertEqual(organizer.get("/api/admin/users?include_guests=1").status_code, 403)
        attempt = organizer.put("/api/admin/users/member_tester/role", json={"role": "Organizer"})
        self.assertEqual(attempt.status_code, 403)
        self.assertEqual(organizer.put("/api/admin/users/organizer_tester/role", json={"role": "Admin"}).status_code, 403)
        self.assertEqual(role_in_db("member_tester"), "User")
        self.assertEqual(role_in_db("organizer_tester"), "Organizer")

    def test_member_types_cannot_manage_events_or_people(self):
        admin = signed_in_client("admin_tester")
        event = admin.post("/api/events", json=SAMPLE_EVENT).get_json()
        try:
            for role in ("User", "Veteran", "Family", "Volunteer"):
                uid = f"{role.lower()}_member"
                make_user(uid, role=role)
                client = signed_in_client(uid)
                responses = {
                    "create event": client.post("/api/events", json=SAMPLE_EVENT),
                    "edit event": client.put(f"/api/events/{event['id']}", json={"title": "Hijacked"}),
                    "delete event": client.delete(f"/api/events/{event['id']}"),
                    "upload picture": client.post("/api/events/image", json={"image": png_base64()}),
                    "list people": client.get("/api/admin/users"),
                    "change role": client.put(f"/api/admin/users/{uid}/role", json={"role": "Admin"}),
                }
                for action, response in responses.items():
                    with self.subTest(role=role, action=action):
                        self.assertEqual(response.status_code, 403)
                self.assertEqual(role_in_db(uid), role)
            stored = [e for e in app.test_client().get("/api/events").get_json() if e["id"] == event["id"]]
            self.assertEqual(stored[0]["title"], SAMPLE_EVENT["title"])
        finally:
            admin.delete(f"/api/events/{event['id']}")

    # ---- roles: sign-up -----------------------------------------------------------

    def signup(self, uid, **extra):
        return app.test_client().post("/api/user/guest", json={
            "name": "Sign Up", "uid": uid, "password": PASSWORD, **extra,
        })

    def test_signup_picks_a_member_type(self):
        veteran = self.signup("signup_veteran", role="Veteran", email="vet@example.org")
        self.assertEqual(veteran.status_code, 200, veteran.get_data(as_text=True))
        self.assertEqual(veteran.get_json()["role"], "Veteran")
        self.assertNotIn("password", veteran.get_json())
        self.assertEqual(signed_in_client("signup_veteran").get("/api/id").get_json()["role"], "Veteran")

        for uid, extra in (("signup_default", {}), ("signup_empty", {"role": ""}), ("signup_null", {"role": None})):
            with self.subTest(uid=uid):
                response = self.signup(uid, **extra)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertEqual(response.get_json()["role"], "User")
        for role in ("Family", "Volunteer", "User"):
            with self.subTest(role=role):
                self.assertEqual(self.signup(f"signup_{role.lower()}_ok", role=role).get_json()["role"], role)

    def test_signup_cannot_pick_staff_or_guest_roles(self):
        for role in ("Admin", "Organizer", "Guest", "Teacher", "veteran", 5):
            with self.subTest(role=role):
                uid = f"signup_bad_{str(role).lower()}"
                response = self.signup(uid, role=role)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.get_json()["message"].startswith("role must be one of: "))
                self.assertIsNone(role_in_db(uid), "no account may be created")

    # ---- roles: survey sets the member type ---------------------------------------

    def survey_client(self, uid, role="User"):
        make_user(uid, role=role)
        return signed_in_client(uid)

    def test_survey_affiliation_sets_member_type(self):
        cases = (
            ("Active duty", "Veteran"),
            ("Family member", "Family"),
            ("Volunteer or supporter", "Volunteer"),
            ("Guard or Reserve", "Veteran"),
            ("Veteran", "Veteran"),
        )
        for index, (affiliation, expected) in enumerate(cases):
            with self.subTest(affiliation=affiliation):
                client = self.survey_client(f"affiliation_{index}")
                response = client.put("/api/profile/survey", json={"affiliation": affiliation})
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertEqual(response.get_json()["affiliation"], affiliation)
                self.assertEqual(client.get("/api/id").get_json()["role"], expected)
        # A later answer moves the account to the new member type
        client = signed_in_client("affiliation_0")
        client.put("/api/profile/survey", json={"affiliation": "Family member"})
        self.assertEqual(role_in_db("affiliation_0"), "Family")

    def test_survey_without_affiliation_leaves_role_alone(self):
        untouched = self.survey_client("survey_other_fields")
        self.assertEqual(untouched.put("/api/profile/survey", json={"branch": "Navy", "bio": "Hi"}).status_code, 200)
        self.assertEqual(role_in_db("survey_other_fields"), "User")

        client = self.survey_client("survey_keeps_role")
        client.put("/api/profile/survey", json={"affiliation": "Active duty"})
        self.assertEqual(role_in_db("survey_keeps_role"), "Veteran")
        client.put("/api/profile/survey", json={"interests": ["Fishing"], "connect_ok": True})
        self.assertEqual(role_in_db("survey_keeps_role"), "Veteran")
        cleared = client.put("/api/profile/survey", json={"affiliation": ""})
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.get_json()["affiliation"])
        self.assertEqual(role_in_db("survey_keeps_role"), "Veteran")

    def test_rejected_survey_changes_neither_survey_nor_role(self):
        client = self.survey_client("survey_rejected")
        client.put("/api/profile/survey", json={"branch": "Army"})
        rejected = client.put("/api/profile/survey", json={
            "affiliation": "Active duty", "branch": "Navy", "service_years": -4,
        })
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("message", rejected.get_json())
        survey = client.get("/api/profile/survey").get_json()
        self.assertIsNone(survey["affiliation"])
        self.assertEqual(survey["branch"], "Army")
        self.assertEqual(role_in_db("survey_rejected"), "User")

        # A rejected first save leaves no survey behind and the role unchanged
        fresh = self.survey_client("survey_rejected_new")
        rejected = fresh.put("/api/profile/survey", json={"affiliation": "Family member", "connect_ok": "yes"})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(fresh.get("/api/profile/survey").get_json(), ProfileSurvey.empty())
        self.assertEqual(role_in_db("survey_rejected_new"), "User")

    def test_survey_never_changes_admin_organizer_guest_or_teacher(self):
        for role in ("Admin", "Organizer", "Teacher"):
            with self.subTest(role=role):
                uid = f"survey_{role.lower()}"
                client = self.survey_client(uid, role=role)
                response = client.put("/api/profile/survey", json={"affiliation": "Veteran"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["affiliation"], "Veteran")
                self.assertEqual(role_in_db(uid), role)
        guest, user = guest_client()
        self.assertEqual(guest.put("/api/profile/survey", json={"affiliation": "Active duty"}).status_code, 200)
        self.assertEqual(guest.get("/api/id").get_json()["role"], "Guest")
        self.assertEqual(role_in_db(user["uid"]), "Guest")


if __name__ == "__main__":
    unittest.main(verbosity=2)
