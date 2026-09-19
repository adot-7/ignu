from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from app import db
from app.ingest import ingest_previous_event, ingest_registrations
from app.mapping import apply, identity_id, load, normalise_github


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ingest"


def test_mapping_normalises_headers_values_urls_and_answers() -> None:
    mapping = load("mapping.sample.yaml")
    row = next(csv.DictReader((FIXTURES / "url_variants.csv").open(newline="")))

    person = apply(row, mapping, "url_variants.csv", 2)

    assert normalise_github("https://github.com/X/", mapping) == "x"
    assert normalise_github("github.com/X", mapping) == "x"
    assert normalise_github("@X", mapping) == "x"
    assert normalise_github("X", mapping) == "x"
    assert normalise_github("not a valid login", mapping) is None
    assert person.name == "Whitespace Person"
    assert person.email == "person@example.test"
    assert person.email_domain == "example.test"
    assert person.github_login == "x"
    assert person.is_student is True
    assert person.answers == {"Why do you want to participate?": "Build useful things"}
    assert person.registered_at is None


def test_amendment_merges_two_pairs_but_not_invalid_in_or_same_names(tmp_db) -> None:
    mapping = load("mapping.yaml")
    report = ingest_registrations(FIXTURES / "amendment.csv", mapping, "ignite")

    assert report.rows == 8
    assert report.with_github == 6
    assert report.persons == 6
    assert report.aliases == 2

    with db.get_session() as session:
        people = list(session.scalars(select(db.PersonRow)).all())
        invalid = [person for person in people if person.name in {"Hemang Dutt", "Krish Gaur"}]
        same_name = [person for person in people if person.name == "Same Name"]
        aliases = [person for person in people if person.alias_of]

    assert len(invalid) == 2
    assert {person.github_login for person in invalid} == {None}
    assert len({person.id for person in invalid}) == 2
    assert len(same_name) == 2
    assert len({person.id for person in same_name}) == 2
    assert len(aliases) == 2
    assert {alias.github_login for alias in aliases} == {"repeat", "second-repeat"}
    assert len({alias.alias_of for alias in aliases}) == 2

    assert identity_id("Hemang Dutt", "Novara Labs", None) == invalid[0].id


def test_sample_csv_persists_participations_and_events(tmp_db) -> None:
    mapping = load("mapping.sample.yaml")
    csv_path = ROOT / "data" / "sample_registrations.csv"
    report = ingest_registrations(csv_path, mapping, "current")

    # The checked-in foundation fixture currently contains one duplicate pair;
    # the amendment fixture above exercises the two-alias shape as well.
    assert report.rows == 60
    assert report.with_github == 9
    assert report.persons == 59
    assert report.aliases == 1
    assert report.students == 3

    with db.get_session() as session:
        people = list(session.scalars(select(db.PersonRow)).all())
        current_parts = list(
            session.scalars(
                select(db.ParticipationRow).where(db.ParticipationRow.event == "current")
            ).all()
        )
        events = list(
            session.scalars(
                select(db.EventRow).where(db.EventRow.stage == "ingest")
            ).all()
        )

    assert len(people) == 60  # aliases are retained as rows with alias_of
    assert len(current_parts) == 59
    assert len(events) == 60
    assert all(event.person_id or event.status == "skip" for event in events)


def test_xlsx_round_trip(tmp_db, tmp_path) -> None:
    mapping = load("mapping.sample.yaml")
    csv_path = ROOT / "data" / "sample_registrations.csv"
    xlsx_path = tmp_path / "registrations.xlsx"
    pd.read_csv(csv_path).to_excel(xlsx_path, index=False)

    report = ingest_registrations(xlsx_path, mapping, "xlsx-event")

    assert report.rows == 60
    assert report.with_github == 9
    assert report.persons == 59
    assert report.aliases == 1


def test_previous_event_matches_email_then_github(tmp_db) -> None:
    mapping = load("mapping.sample.yaml")
    ingest_registrations(ROOT / "data" / "sample_registrations.csv", mapping, "current")

    participations = ingest_previous_event(FIXTURES / "previous_event.csv", mapping, "past")

    assert len(participations) == 3
    with db.get_session() as session:
        rows = list(
            session.scalars(
                select(db.ParticipationRow).where(db.ParticipationRow.event == "past")
            ).all()
        )
        people = {
            person.id: person
            for person in session.scalars(select(db.PersonRow)).all()
        }
    by_email = {people[participation.person_id].email: participation for participation in participations}
    assert by_email["team.asha@example.org"].checked_in is True
    assert by_email["team.asha@example.org"].submitted is True
    assert by_email["team.asha@example.org"].placed == 1
    assert by_email["rohan.sample@example.org"].checked_in is False
    assert by_email["meera.sample@example.org"].checked_in is False
    assert len(rows) == 3
    assert {row.team_id for row in rows} == {"team-alpha", "team-beta"}
