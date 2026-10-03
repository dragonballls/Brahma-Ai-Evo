        return calls

    def _call_llm(self) -> str:
        """Use OmniRoute's coding route first, then retain direct-provider compatibility fallback."""
        try:
            from llm_client import client as ai_client
            response = ai_client.multi_turn(
                self.history,
                model="auto/coding",
                max_tokens=8192,
                temperature=0.2,
            )
            if response:
                return response.strip()
        except Exception as exc:
            logger.warning(f"[BrahmaDev] OmniRoute coding route failed: {exc}")

        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                keys = json.load(f)
            gemini_key = keys.get("gemini_api_key", "").strip()

            if gemini_key:
                import time
                from google import genai
                from google.genai import types

                client = genai.Client(api_key=gemini_key)

                system_instruction = BRAHMA_DEV_SYSTEM_PROMPT
                contents = []
                for msg in self.history:
                    if msg["role"] == "system":
                        system_instruction = msg["content"]
                    else:
                        role = "user" if msg["role"] == "user" else "model"
                        contents.append(types.Content(
                            role=role,
                            parts=[types.Part.from_text(text=msg["content"])]
                        ))

                # Attempt primary model and fallback model with retries
                models_to_try = ["gemini-2.5-flash", "gemini-3.6-flash"]
                last_err = None

                for model_name in models_to_try:
                    for attempt in range(3):
                        try:
                            resp = client.models.generate_content(
                                model=model_name,
                                contents=contents,
                                config={"system_instruction": system_instruction, "temperature": 0.2}
                            )
                            if resp.text:
                                return resp.text.strip()
                        except Exception as e:
                            last_err = e
                            err_str = str(e).lower()
                            if "429" in err_str or "quota" in err_str or "resource_exhausted" in err_str or "503" in err_str:
                                time.sleep(2 * (attempt + 1))
                                continue
                            else:
                                break

                if last_err:
                    logger.warning(f"[BrahmaDev] Gemini calls exhausted: {last_err}")
        except Exception as e:
            logger.warning(f"[BrahmaDev] Direct Gemini setup error: {e}")

        # Check if openrouter key actually exists before falling back
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                or_key = json.load(f).get("openrouter_api_key", "").strip()
            if or_key:
                from llm_client import client as ai_client
                return ai_client.multi_turn(self.history, temperature=0.2)
        except Exception: