from django.test import SimpleTestCase
from unittest.mock import patch

from listings.ai_assistant import (
    _build_retrieval_context,
    _respond_with_gemini_final,
    assistant_reply,
)


class AssistantIntentMatrixTests(SimpleTestCase):
    def test_build_retrieval_context_includes_structured_items(self):
        ctx = _build_retrieval_context(
            retrieval_text="Found relevant options.",
            retrieval_items=[
                {
                    "type": "listing",
                    "id": 7,
                    "title": "Sample Listing",
                    "price": "1299.00",
                    "reason": "Matches user budget.",
                    "url": "/listings/listing/7/",
                }
            ],
        )
        self.assertIn("Retriever summary: Found relevant options.", ctx)
        self.assertIn("listing: Sample Listing", ctx)
        self.assertIn("price=1299.00", ctx)

    @patch("listings.ai_assistant._generate_gemini_text", return_value="Model-final answer.")
    def test_respond_with_gemini_final_prefers_model_output(self, gemini_mock):
        res = _respond_with_gemini_final(
            user_prompt="What should I buy?",
            base_prompt="System prompt here",
            retrieval_text="Found 2 listings.",
            retrieval_items=[],
            fallback_text="Fallback text",
        )
        self.assertEqual(res["text"], "Model-final answer.")
        gemini_mock.assert_called_once()

    @patch("listings.ai_assistant._answer_from_db")
    @patch("listings.ai_assistant._generate_gemini_text", return_value="Gemini polished output.")
    def test_assistant_reply_routes_db_results_to_gemini_final(self, gemini_mock, db_mock):
        db_mock.return_value = {
            "text": "Most expensive item: Mercedes Benz E200 - 7900000.00",
            "platform_items": [
                {
                    "type": "listing",
                    "id": 5,
                    "title": "Mercedes Benz E200",
                    "price": "7900000.00",
                    "url": "/listings/listing/5/",
                }
            ],
        }
        res = assistant_reply("what is the most expensive commodity in baysoko", user_id=None)
        self.assertEqual(res["text"], "Gemini polished output.")
        gemini_mock.assert_called_once()

    @patch("listings.ai_assistant._answer_from_db")
    @patch("listings.ai_assistant._generate_gemini_text", return_value=None)
    def test_assistant_reply_uses_retrieval_fallback_when_gemini_unavailable(self, _gemini_mock, db_mock):
        db_mock.return_value = {
            "text": "Found 3 store(s).",
            "platform_items": [
                {"type": "store", "id": 1, "name": "Undugu Ultimate Online Stores", "url": "/store/undugu/"}
            ],
        }
        res = assistant_reply("what stores do i own", user_id=None)
        self.assertIn("Found 3 store(s).", res["text"])
