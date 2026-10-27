"""SSE framing and the per-session stream limit (no database)."""

import json
import unittest
from unittest.mock import MagicMock, call, patch

from api.events import StreamLimiter, frame


class FrameTests(unittest.TestCase):

    def test_frame_has_id_event_and_one_json_data_line(self):
        text = frame("changes", {"changes": [{"operation_id": "x"}]}, "42")
        self.assertTrue(text.endswith("\n\n"))
        lines = text.strip().split("\n")
        self.assertEqual(lines[0], "id: 42")
        self.assertEqual(lines[1], "event: changes")
        self.assertEqual(json.loads(lines[2][len("data: "):]),
                         {"changes": [{"operation_id": "x"}]})

    def test_data_never_spans_lines(self):
        text = frame("changes", {"note": "a\nb\n\nevent: forged"})
        lines = text.strip().split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(sum(l.startswith("data:") for l in lines), 1)
        self.assertEqual(sum(l.startswith("event:") for l in lines), 1)


class LimiterTests(unittest.TestCase):

    def test_limit_per_key_and_release(self):
        limiter = StreamLimiter()
        self.assertTrue(all(limiter.acquire("a", 3) for _ in range(3)))
        self.assertFalse(limiter.acquire("a", 3))
        self.assertTrue(limiter.acquire("b", 3))
        limiter.release("a")
        self.assertTrue(limiter.acquire("a", 3))


class JourneyReconnectTests(unittest.TestCase):

    def test_reconnect_before_first_change_preserves_cursor_and_closes_stream(self):
        from tests.e2e_api.test_journeys import JourneyTests

        journey = JourneyTests("test_connection_interruption_catches_up_from_last_event_id")
        streams = [MagicMock(), MagicMock()]
        for stream, payload in zip(streams, (
            frame("reconnect", {}, "42"),
            frame("changes", {"changes": [{"operation_id": "missed"}]}, "43"),
        )):
            response = stream.__enter__.return_value
            response.status_code = 200
            response.iter_lines.return_value = iter(payload.splitlines())
        person = MagicMock()
        person.http.stream.side_effect = streams

        with patch.object(journey, "person", return_value=person):
            seen, cursor = journey.follow("investigator", "41", lambda ids: "missed" in ids)

        self.assertEqual(seen, {"missed"})
        self.assertEqual(cursor, "43")
        self.assertEqual(person.http.stream.call_args_list, [
            call("GET", "/events", headers={"Last-Event-ID": "41"}),
            call("GET", "/events", headers={"Last-Event-ID": "42"}),
        ])
        for stream in streams:
            stream.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
