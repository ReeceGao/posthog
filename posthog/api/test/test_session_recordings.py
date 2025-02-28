import uuid
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import ANY, patch, MagicMock, call
from urllib.parse import urlencode

from parameterized import parameterized
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta
from django.utils.timezone import now
from freezegun import freeze_time
from rest_framework import status

from posthog.api.session_recording import DEFAULT_RECORDING_CHUNK_LIMIT
from posthog.api.test.test_team import create_team
from posthog.models import Organization, Person, SessionRecording
from posthog.models.filters.session_recordings_filter import SessionRecordingsFilter
from posthog.models.session_recording_event import SessionRecordingViewed
from posthog.models.team import Team
from posthog.queries.session_recordings.test.session_replay_sql import produce_replay_summary
from posthog.session_recordings.test.test_factory import create_session_recording_events
from posthog.test.base import (
    APIBaseTest,
    ClickhouseTestMixin,
    QueryMatchingTest,
    flush_persons_and_events,
    snapshot_postgres_queries,
    FuzzyInt,
)


class TestSessionRecordings(APIBaseTest, ClickhouseTestMixin, QueryMatchingTest):
    def setUp(self):
        super().setUp()

        # Create a new team each time to ensure no clashing between tests
        self.team = Team.objects.create(organization=self.organization, name="New Team")

    def create_snapshot(
        self,
        distinct_id,
        session_id,
        timestamp,
        team_id=None,
        window_id="",
        source=0,
        has_full_snapshot=True,
        type=2,
        snapshot_data=None,
        use_replay_table=True,
        use_recording_table=False,
    ):
        if team_id is None:
            team_id = self.team.pk

        snapshot = {
            "timestamp": timestamp.timestamp() * 1000,
            "has_full_snapshot": has_full_snapshot,
            "type": type,
            "data": {"source": source},
        }

        if snapshot_data:
            snapshot.update(snapshot_data)

        create_session_recording_events(
            team_id=team_id,
            distinct_id=distinct_id,
            timestamp=timestamp,
            session_id=session_id,
            window_id=window_id,
            snapshots=[snapshot],
            use_replay_table=use_replay_table,
            use_recording_table=use_recording_table,
        )

    def create_snapshots(
        self,
        snapshot_count,
        distinct_id,
        session_id,
        timestamp,
        has_full_snapshot=True,
        window_id="",
        use_replay_table=True,
        use_recording_table=False,
    ):
        snapshots = []
        for index in range(snapshot_count):
            snapshots.append(
                {
                    "type": 2 if has_full_snapshot else 3,
                    "data": {
                        "source": 0,
                        "texts": [],
                        "attributes": [],
                        "removes": [],
                        "adds": [
                            {
                                "parentId": 4,
                                "nextId": 386,
                                "node": {
                                    "type": 2,
                                    "tagName": "style",
                                    "attributes": {"data-emotion": "css"},
                                    "childNodes": [],
                                    "id": 729,
                                },
                            }
                        ],
                    },
                    "timestamp": (timestamp + timedelta(seconds=index)).timestamp() * 1000,
                }
            )

        create_session_recording_events(
            team_id=self.team.pk,
            distinct_id=distinct_id,
            timestamp=timestamp,
            session_id=session_id,
            window_id=window_id,
            snapshots=snapshots,
            use_replay_table=use_replay_table,
            use_recording_table=use_recording_table,
        )

    def test_get_session_recordings(self):
        user = Person.objects.create(
            team=self.team, distinct_ids=["user"], properties={"$some_prop": "something", "email": "bob@bob.com"}
        )
        user2 = Person.objects.create(
            team=self.team, distinct_ids=["user2"], properties={"$some_prop": "something", "email": "bob@bob.com"}
        )
        base_time = (now() - relativedelta(days=1)).replace(microsecond=0)
        session_id_one = f"test_get_session_recordings-1-{uuid.uuid4()}"
        self.create_snapshot("user", session_id_one, base_time)
        self.create_snapshot("user", session_id_one, base_time + relativedelta(seconds=10))
        self.create_snapshot("user", session_id_one, base_time + relativedelta(seconds=30))
        session_id_two = f"test_get_session_recordings-2-{uuid.uuid4()}"
        self.create_snapshot("user2", session_id_two, base_time + relativedelta(seconds=20))

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()

        assert [
            (
                r["id"],
                r["distinct_id"],
                parse(r["start_time"]),
                parse(r["end_time"]),
                r["recording_duration"],
                r["viewed"],
                r["person"]["id"],
            )
            for r in response_data["results"]
        ] == [
            (
                session_id_two,
                "user2",
                base_time + relativedelta(seconds=20),
                base_time + relativedelta(seconds=20),
                0,
                False,
                user2.pk,
            ),
            (session_id_one, "user", base_time, base_time + relativedelta(seconds=30), 30, False, user.pk),
        ]

    @patch("posthog.api.session_recording.SessionRecordingListFromReplaySummary")
    def test_console_log_filters_are_correctly_passed_to_listing(self, mock_summary_lister):
        self.client.get(f'/api/projects/{self.team.id}/session_recordings?console_logs=["warn", "error"]')
        assert len(mock_summary_lister.call_args_list) == 1
        filter_passed_to_mock: SessionRecordingsFilter = mock_summary_lister.call_args_list[0].kwargs["filter"]
        assert filter_passed_to_mock.console_logs_filter == ["warn", "error"]

    @snapshot_postgres_queries
    def test_listing_recordings_is_not_nplus1_for_persons(self):
        with freeze_time("2022-06-03T12:00:00.000Z"):
            # request once without counting queries to cache an ee.license lookup that makes results vary otherwise
            self.client.get(f"/api/projects/{self.team.id}/session_recordings")

            base_time = (now() - relativedelta(days=1)).replace(microsecond=0)
            num_queries = FuzzyInt(12, 19)  # PoE on or off adds queries here :shrug:

            # loop from 1 to 10
            for i in range(1, 11):
                self._person_with_snapshots(
                    base_time=base_time,
                    distinct_id=f"user{i}",
                    session_id=f"{i}",
                )
                with self.assertNumQueries(num_queries):
                    self.client.get(f"/api/projects/{self.team.id}/session_recordings")

    def _person_with_snapshots(self, base_time: datetime, distinct_id: str = "user", session_id: str = "1") -> None:
        Person.objects.create(
            team=self.team, distinct_ids=[distinct_id], properties={"$some_prop": "something", "email": "bob@bob.com"}
        )
        self.create_snapshot(distinct_id, session_id, base_time)
        self.create_snapshot(distinct_id, session_id, base_time + relativedelta(seconds=10))
        flush_persons_and_events()

    def test_session_recordings_dont_leak_teams(self) -> None:
        another_team = Team.objects.create(organization=self.organization)
        Person.objects.create(
            team=another_team, distinct_ids=["user"], properties={"$some_prop": "something", "email": "bob@bob.com"}
        )
        Person.objects.create(
            team=self.team, distinct_ids=["user"], properties={"$some_prop": "something", "email": "bob@bob.com"}
        )

        base_time = (now() - relativedelta(days=1)).replace(microsecond=0)
        self.create_snapshot("user", "1", base_time, team_id=another_team.pk)
        self.create_snapshot("user", "2", base_time)

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(len(response_data["results"]), 1)
        self.assertEqual(response_data["results"][0]["id"], "2")

    def test_session_recording_for_user_with_multiple_distinct_ids(self) -> None:
        base_time = (now() - timedelta(days=1)).replace(microsecond=0)
        p = Person.objects.create(
            team=self.team,
            distinct_ids=["d1", "d2"],
            properties={"$some_prop": "something", "email": "bob@bob.com"},
        )
        self.create_snapshot("d1", "1", base_time)
        self.create_snapshot("d2", "2", base_time + relativedelta(seconds=30))
        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings")
        response_data = response.json()
        self.assertEqual(len(response_data["results"]), 2)
        self.assertEqual(response_data["results"][0]["person"]["id"], p.pk)
        self.assertEqual(response_data["results"][1]["person"]["id"], p.pk)

    def test_viewed_state_of_session_recording_version_1(self):
        Person.objects.create(
            team=self.team, distinct_ids=["u1"], properties={"$some_prop": "something", "email": "bob@bob.com"}
        )
        base_time = (now() - timedelta(days=1)).replace(microsecond=0)
        SessionRecordingViewed.objects.create(team=self.team, user=self.user, session_id="1")
        self.create_snapshot("u1", "1", base_time, use_recording_table=True)
        self.create_snapshot("u1", "2", base_time + relativedelta(seconds=30), use_recording_table=True)
        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings")
        response_data = response.json()
        self.assertEqual(len(response_data["results"]), 2)
        self.assertEqual(response_data["results"][0]["id"], "2")
        self.assertEqual(response_data["results"][0]["viewed"], False)
        self.assertEqual(response_data["results"][1]["id"], "1")
        self.assertEqual(response_data["results"][1]["viewed"], True)

    def test_viewed_state_of_session_recording_version_3(self):
        Person.objects.create(
            team=self.team, distinct_ids=["u1"], properties={"$some_prop": "something", "email": "bob@bob.com"}
        )
        base_time = (now() - timedelta(days=1)).replace(microsecond=0)
        SessionRecordingViewed.objects.create(team=self.team, user=self.user, session_id="1")
        self.create_snapshot("u1", "1", base_time)
        self.create_snapshot("u1", "2", base_time + relativedelta(seconds=30))
        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings")
        response_data = response.json()
        self.assertEqual(len(response_data["results"]), 2)
        self.assertEqual(response_data["results"][0]["id"], "2")
        self.assertEqual(response_data["results"][0]["viewed"], False)
        self.assertEqual(response_data["results"][1]["id"], "1")
        self.assertEqual(response_data["results"][1]["viewed"], True)

    def test_setting_viewed_state_of_session_recording(self):
        Person.objects.create(
            team=self.team, distinct_ids=["u1"], properties={"$some_prop": "something", "email": "bob@bob.com"}
        )
        base_time = (now() - relativedelta(days=1)).replace(microsecond=0)

        produce_replay_summary(
            session_id="1",
            team_id=self.team.pk,
            first_timestamp=base_time.isoformat(),
            last_timestamp=base_time.isoformat(),
            distinct_id="u1",
            first_url="https://example.io/home",
            click_count=2,
            keypress_count=2,
            mouse_activity_count=2,
            active_milliseconds=50 * 1000 * 0.5,
        )

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings")
        response_data = response.json()
        # Make sure it starts not viewed
        assert response_data["results"][0]["viewed"] is False
        assert response_data["results"][0]["id"] == "1"

        # can get it directly
        get_session_response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/1")
        assert get_session_response.status_code == 200
        assert get_session_response.json()["viewed"] is False
        assert get_session_response.json()["id"] == "1"

        # being loaded doesn't mark it as viewed
        all_sessions_response = self.client.get(f"/api/projects/{self.team.id}/session_recordings")
        response_data = all_sessions_response.json()
        # Make sure it remains not viewed
        assert response_data["results"][0]["viewed"] is False
        assert response_data["results"][0]["id"] == "1"

        # can set it to viewed
        save_as_viewed_response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/1?save_view=True")
        assert save_as_viewed_response.status_code == 200

        final_view_response = self.client.get(f"/api/projects/{self.team.id}/session_recordings")
        response_data = final_view_response.json()
        # Make sure the query param sets it to viewed
        assert response_data["results"][0]["viewed"] is True
        assert response_data["results"][0]["id"] == "1"

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/1")
        response_data = response.json()
        # In the metadata response too
        self.assertEqual(response_data["viewed"], True)

    def test_get_single_session_recording_metadata(self):
        with freeze_time("2023-01-01T12:00:00.000Z"):
            p = Person.objects.create(
                team=self.team, distinct_ids=["d1"], properties={"$some_prop": "something", "email": "bob@bob.com"}
            )
            session_recording_id = "session_1"
            base_time = (now() - relativedelta(days=1)).replace(microsecond=0)
            produce_replay_summary(
                session_id=session_recording_id,
                team_id=self.team.pk,
                first_timestamp=base_time.isoformat(),
                last_timestamp=(base_time + relativedelta(seconds=30)).isoformat(),
                distinct_id="d1",
            )

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/{session_recording_id}")
        response_data = response.json()

        assert response_data == {
            "id": "session_1",
            "distinct_id": "d1",
            "viewed": False,
            "pinned_count": 0,
            "recording_duration": 30,
            "start_time": base_time.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time": (base_time + relativedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "click_count": 0,
            "keypress_count": 0,
            "start_url": None,
            "mouse_activity_count": None,
            "inactive_seconds": None,
            "active_seconds": None,
            "console_error_count": None,
            "console_log_count": None,
            "console_warn_count": None,
            "person": {
                "id": p.id,
                "name": "bob@bob.com",
                "distinct_ids": ["d1"],
                "properties": {"email": "bob@bob.com", "$some_prop": "something"},
                "created_at": "2023-01-01T12:00:00Z",
                "uuid": ANY,
            },
            "storage": "clickhouse",
        }

    def test_get_default_limit_of_chunks(self):
        base_time = now()
        num_snapshots = DEFAULT_RECORDING_CHUNK_LIMIT + 10

        for _ in range(num_snapshots):
            self.create_snapshot("user", "1", base_time, use_recording_table=True, use_replay_table=False)

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/1/snapshots")
        response_data = response.json()
        self.assertEqual(len(response_data["snapshot_data_by_window_id"][""]), DEFAULT_RECORDING_CHUNK_LIMIT)

    def test_get_snapshots_is_compressed(self):
        base_time = now()
        num_snapshots = 2  # small contents aren't compressed, needs to be enough data to trigger compression

        for _ in range(num_snapshots):
            self.create_snapshot("user", "1", base_time, use_recording_table=True)

        custom_headers = {"HTTP_ACCEPT_ENCODING": "gzip"}
        response = self.client.get(
            f"/api/projects/{self.team.id}/session_recordings/1/snapshots",
            data=None,
            follow=False,
            secure=False,
            **custom_headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.headers.get("Content-Encoding", None), "gzip")

    def test_get_snapshots_for_chunked_session_recording(self):
        chunked_session_id = "chunk_id"
        expected_num_requests = 3
        num_chunks = 60
        snapshots_per_chunk = 2

        with freeze_time("2020-09-13T12:26:40.000Z"):
            start_time = now()
            for index, s in enumerate(range(num_chunks)):
                self.create_snapshots(
                    snapshots_per_chunk,
                    "user",
                    chunked_session_id,
                    start_time + relativedelta(minutes=s),
                    window_id="1" if index % 2 == 0 else "2",
                    use_recording_table=True,
                    use_replay_table=False,
                )

            next_url = f"/api/projects/{self.team.id}/session_recordings/{chunked_session_id}/snapshots"

            for i in range(expected_num_requests):
                response = self.client.get(next_url)
                response_data = response.json()

                self.assertEqual(
                    len(response_data["snapshot_data_by_window_id"]["1"]),
                    snapshots_per_chunk * DEFAULT_RECORDING_CHUNK_LIMIT / 2,
                )
                self.assertEqual(
                    len(response_data["snapshot_data_by_window_id"]["2"]),
                    snapshots_per_chunk * DEFAULT_RECORDING_CHUNK_LIMIT / 2,
                )
                if i == expected_num_requests - 1:
                    self.assertIsNone(response_data["next"])
                else:
                    self.assertIsNotNone(response_data["next"])

                next_url = response_data["next"]

    def test_single_session_recording_doesnt_leak_teams(self):
        another_team = Team.objects.create(organization=self.organization)
        self.create_snapshot("user", "id_no_team_leaking", now() - relativedelta(days=1), team_id=another_team.pk)
        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/id_no_team_leaking")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/id_no_team_leaking/snapshots")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_session_recording_with_no_person(self):
        produce_replay_summary(
            session_id="id_no_person",
            team_id=self.team.pk,
            first_timestamp=(now() - relativedelta(days=1)).isoformat(),
            last_timestamp=(now() - relativedelta(days=1)).isoformat(),
            distinct_id="d1",
        )

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/id_no_person")
        response_data = response.json()
        self.assertEqual(response_data["person"], None)

    def test_session_recording_doesnt_exist(self):
        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/non_existent_id")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/non_existent_id/snapshots")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_request_to_another_teams_endpoint_returns_401(self):
        org = Organization.objects.create(name="Separate Org")
        another_team = Team.objects.create(organization=org)
        self.create_snapshot("user", "id_no_team_leaking", now() - relativedelta(days=1), team_id=another_team.pk)
        response = self.client.get(f"/api/projects/{another_team.pk}/session_recordings/id_no_team_leaking")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @parameterized.expand(
        [
            (False, 3),
            (True, 1),
        ]
    )
    def test_session_ids_filter(self, use_recording_events: bool, api_version: int):
        with freeze_time("2020-09-13T12:26:40.000Z"):
            Person.objects.create(
                team=self.team, distinct_ids=["user"], properties={"$some_prop": "something", "email": "bob@bob.com"}
            )
            self.create_snapshot("user", "1", now() - relativedelta(days=1), use_recording_table=use_recording_events)
            self.create_snapshot("user", "2", now() - relativedelta(days=2), use_recording_table=use_recording_events)
            self.create_snapshot("user", "3", now() - relativedelta(days=3), use_recording_table=use_recording_events)

            # Fetch playlist
            params_string = urlencode({"session_ids": '["1", "2", "3"]', "version": api_version})
            response = self.client.get(f"/api/projects/{self.team.id}/session_recordings?{params_string}")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            response_data = response.json()

            self.assertEqual(len(response_data["results"]), 3)
            self.assertEqual(response_data["results"][0]["id"], "1")
            self.assertEqual(response_data["results"][1]["id"], "2")
            self.assertEqual(response_data["results"][2]["id"], "3")

    def test_empty_list_session_ids_filter_returns_no_recordings(self):
        with freeze_time("2020-09-13T12:26:40.000Z"):
            Person.objects.create(
                team=self.team, distinct_ids=["user"], properties={"$some_prop": "something", "email": "bob@bob.com"}
            )
            self.create_snapshot("user", "1", now() - relativedelta(days=1))
            self.create_snapshot("user", "2", now() - relativedelta(days=2))
            self.create_snapshot("user", "3", now() - relativedelta(days=3))

            # Fetch playlist
            params_string = urlencode({"session_ids": "[]"})
            response = self.client.get(f"/api/projects/{self.team.id}/session_recordings?{params_string}")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            response_data = response.json()

            self.assertEqual(len(response_data["results"]), 0)

    def test_regression_encoded_emojis_dont_crash(self):

        Person.objects.create(
            team=self.team, distinct_ids=["user"], properties={"$some_prop": "something", "email": "bob@bob.com"}
        )
        with freeze_time("2022-01-01T12:00:00.000Z"):
            self.create_snapshot(
                "user",
                "1",
                now() - relativedelta(days=1),
                # TODO do we need a version of this that writes to blob storage?
                snapshot_data={"texts": ["\\ud83d\udc83\\ud83c\\udffb"]},  # This is an invalid encoded emoji
                use_recording_table=True,
            )

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/1/snapshots")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()

        assert not response_data["next"]
        assert response_data["snapshot_data_by_window_id"] == {
            "": [
                {
                    "texts": ["\\ud83d\udc83\\ud83c\\udffb"],
                    "timestamp": 1640952000000.0,
                    "has_full_snapshot": True,
                    "type": 2,
                    "data": {"source": 0},
                }
            ]
        }

    def test_delete_session_recording(self):
        self.create_snapshot("user", "1", now() - relativedelta(days=1), team_id=self.team.pk)
        response = self.client.delete(f"/api/projects/{self.team.id}/session_recordings/1")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        # Trying to delete same recording again returns 404
        response = self.client.delete(f"/api/projects/{self.team.id}/session_recordings/1")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # New snapshot loading method
    @freeze_time("2023-01-01T00:00:00Z")
    @patch("posthog.api.session_recording.object_storage.list_objects")
    def test_get_snapshots_v2_default_response(self, mock_list_objects) -> None:
        session_id = str(uuid.uuid4())
        timestamp = round(now().timestamp() * 1000)
        mock_list_objects.return_value = [
            f"session_recordings/team_id/{self.team.pk}/session_id/{session_id}/data/{timestamp - 10000}-{timestamp - 5000}",
            f"session_recordings/team_id/{self.team.pk}/session_id/{session_id}/data/{timestamp - 5000}-{timestamp}",
        ]
        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/{session_id}/snapshots?version=2")
        response_data = response.json()

        assert response_data == {
            "sources": [
                {
                    "source": "blob",
                    "start_timestamp": "2022-12-31T23:59:50Z",
                    "end_timestamp": "2022-12-31T23:59:55Z",
                    "blob_key": "1672531190000-1672531195000",
                },
                {
                    "source": "blob",
                    "start_timestamp": "2022-12-31T23:59:55Z",
                    "end_timestamp": "2023-01-01T00:00:00Z",
                    "blob_key": "1672531195000-1672531200000",
                },
                {
                    "source": "realtime",
                    "start_timestamp": "2022-12-31T23:59:55Z",
                    "end_timestamp": None,
                    "blob_key": None,
                },
            ]
        }
        mock_list_objects.assert_called_with(f"session_recordings/team_id/{self.team.pk}/session_id/{session_id}/data")

    @freeze_time("2023-01-01T00:00:00Z")
    @patch("posthog.api.session_recording.object_storage.list_objects")
    def test_get_snapshots_upgrade_to_v2_if_stored_recording_requires_it(self, mock_list_objects: MagicMock) -> None:
        session_id = str(uuid.uuid4())
        timestamp = round(now().timestamp() * 1000)
        mock_list_objects.return_value = [
            f"session_recordings/team_id/{self.team.pk}/session_id/{session_id}/data/{timestamp - 10000}-{timestamp - 5000}",
            f"session_recordings/team_id/{self.team.pk}/session_id/{session_id}/data/{timestamp - 5000}-{timestamp}",
        ]

        # if the recording has been written with a newer version, we have to upgrade to v2
        SessionRecording.objects.create(team=self.team, session_id=session_id, storage_version="2023-08-01")

        # add an unnecessary param to make sure we maintain params when redirecting
        response = self.client.get(
            f"/api/projects/{self.team.id}/session_recordings/{session_id}/snapshots?some-param=1"
        )
        assert response.status_code == status.HTTP_302_FOUND
        assert (
            response.headers["Location"]
            == f"/api/projects/{self.team.id}/session_recordings/{session_id}/snapshots?some-param=1&version=2"
        )

        mock_list_objects.assert_not_called()

    @freeze_time("2023-01-01T00:00:00Z")
    @patch("posthog.api.session_recording.object_storage.list_objects")
    def test_get_snapshots_v2_from_lts(self, mock_list_objects: MagicMock) -> None:
        session_id = str(uuid.uuid4())
        timestamp = round(now().timestamp() * 1000)

        SessionRecording.objects.create(
            team=self.team,
            session_id=session_id,
            deleted=False,
            storage_version="2023-08-01",
            object_storage_path="an lts stored object path",
        )

        def list_objects_func(path: str) -> List[str]:
            # this mock simulates a recording whose blob storage has been deleted by TTL
            # but which has been stored in LTS blob storage
            if path == "an lts stored object path":
                return [
                    f"an lts stored object path/{timestamp - 10000}-{timestamp - 5000}",
                    f"an lts stored object path/{timestamp - 5000}-{timestamp}",
                ]
            else:
                return []

        mock_list_objects.side_effect = list_objects_func

        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/{session_id}/snapshots?version=2")
        response_data = response.json()

        assert response_data == {
            "sources": [
                {
                    "source": "blob",
                    "start_timestamp": "2022-12-31T23:59:50Z",
                    "end_timestamp": "2022-12-31T23:59:55Z",
                    "blob_key": "1672531190000-1672531195000",
                },
                {
                    "source": "blob",
                    "start_timestamp": "2022-12-31T23:59:55Z",
                    "end_timestamp": "2023-01-01T00:00:00Z",
                    "blob_key": "1672531195000-1672531200000",
                },
                {
                    "source": "realtime",
                    "start_timestamp": "2022-12-31T23:59:55Z",
                    "end_timestamp": None,
                    "blob_key": None,
                },
            ]
        }
        assert mock_list_objects.call_args_list == [
            call(f"session_recordings/team_id/{self.team.pk}/session_id/{session_id}/data"),
            call("an lts stored object path"),
        ]

    @freeze_time("2023-01-01T00:00:00Z")
    @patch("posthog.api.session_recording.object_storage.list_objects")
    def test_get_snapshots_v2_default_response_no_realtime_if_old(self, mock_list_objects) -> None:
        session_id = str(uuid.uuid4())
        old_timestamp = round((now() - timedelta(hours=26)).timestamp() * 1000)

        mock_list_objects.return_value = [
            f"session_recordings/team_id/{self.team.pk}/session_id/{session_id}/data/{old_timestamp - 10000}-{old_timestamp}",
        ]
        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/{session_id}/snapshots?version=2")
        response_data = response.json()

        assert response_data == {
            "sources": [
                {
                    "source": "blob",
                    "start_timestamp": "2022-12-30T21:59:50Z",
                    "end_timestamp": "2022-12-30T22:00:00Z",
                    "blob_key": "1672437590000-1672437600000",
                }
            ]
        }

    @patch("posthog.api.session_recording.SessionRecording.get_or_build")
    @patch("posthog.api.session_recording.object_storage.get_presigned_url")
    @patch("posthog.api.session_recording.requests")
    def test_can_get_session_recording_blob(
        self, _mock_requests, mock_presigned_url, mock_get_session_recording
    ) -> None:
        session_id = str(uuid.uuid4())
        """API will add session_recordings/team_id/{self.team.pk}/session_id/{session_id}"""
        blob_key = f"1682608337071"
        url = f"/api/projects/{self.team.pk}/session_recordings/{session_id}/snapshots/?version=2&source=blob&blob_key={blob_key}"

        # by default a session recording is deleted, so we have to explicitly mark the mock as not deleted
        mock_get_session_recording.return_value = SessionRecording(session_id=session_id, team=self.team, deleted=False)

        def presigned_url_sideeffect(key: str, **kwargs):
            if key == f"session_recordings/team_id/{self.team.pk}/session_id/{session_id}/data/{blob_key}":
                return f"https://test.com/"
            else:
                return None

        mock_presigned_url.side_effect = presigned_url_sideeffect

        response = self.client.get(url)
        assert response.status_code == status.HTTP_200_OK

    @patch("posthog.api.session_recording.SessionRecording.get_or_build")
    @patch("posthog.api.session_recording.object_storage.get_presigned_url")
    @patch("posthog.api.session_recording.requests")
    def test_cannot_get_session_recording_blob_for_made_up_sessions(
        self, _mock_requests, mock_presigned_url, mock_get_session_recording
    ) -> None:
        session_id = str(uuid.uuid4())
        blob_key = f"1682608337071"
        url = f"/api/projects/{self.team.pk}/session_recordings/{session_id}/snapshots/?version=2&source=blob&blob_key={blob_key}"

        # by default a session recording is deleted, and _that_ is what we check for to see if it exists
        # so, we have to explicitly mark the mock as deleted
        mock_get_session_recording.return_value = SessionRecording(session_id=session_id, team=self.team, deleted=True)

        response = self.client.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert mock_presigned_url.call_count == 0

    @patch("posthog.api.session_recording.object_storage.get_presigned_url")
    def test_can_not_get_session_recording_blob_that_does_not_exist(self, mock_presigned_url) -> None:
        session_id = str(uuid.uuid4())
        blob_key = f"session_recordings/team_id/{self.team.pk}/session_id/{session_id}/data/1682608337071"
        url = f"/api/projects/{self.team.pk}/session_recordings/{session_id}/snapshots/?version=2&source=blob&blob_key={blob_key}"

        mock_presigned_url.return_value = None

        response = self.client.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @parameterized.expand(
        [
            (False, 3),
            (True, 1),
        ]
    )
    def test_get_via_sharing_token(self, use_recording_events: bool, api_version: int) -> None:
        other_team = create_team(organization=self.organization)

        session_id = str(uuid.uuid4())
        with freeze_time("2023-01-01T12:00:00Z"):
            self.create_snapshot(
                "user",
                session_id,
                now() - relativedelta(days=1),
                team_id=self.team.pk,
                use_recording_table=use_recording_events,
            )

        token = self.client.patch(
            f"/api/projects/{self.team.id}/session_recordings/{session_id}/sharing", {"enabled": True}
        ).json()["access_token"]

        self.client.logout()

        # Unallowed routes
        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings/2?sharing_access_token={token}")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.get(f"/api/projects/{self.team.id}/session_recordings?sharing_access_token={token}")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.get(f"/api/projects/12345/session_recordings?sharing_access_token={token}")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.get(
            f"/api/projects/{other_team.id}/session_recordings/{session_id}?sharing_access_token={token}"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        response = self.client.get(
            f"/api/projects/{self.team.id}/session_recordings/{session_id}?sharing_access_token={token}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        assert response.json() == {
            "id": session_id,
            "recording_duration": 0,
            "start_time": "2022-12-31T12:00:00Z",
            "end_time": "2022-12-31T12:00:00Z",
        }

        # if api_version is three then we should request snapshots with version 2
        response = self.client.get(
            f"/api/projects/{self.team.id}/session_recordings/{session_id}/snapshots?sharing_access_token={token}&version={api_version-1}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
