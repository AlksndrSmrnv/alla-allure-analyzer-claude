"""Inherited CLI environments without unrelated credentials or process injection."""
from __future__ import annotations

import os
from collections.abc import Mapping

SERVICE_PREFIXES = ("ALLURE_", "QWEN_CODE_", "GIT_", "GH_", "GITHUB_", "GITLAB_",
                    "TRACKER_", "JIRA_", "YOUTRACK_")
INJECTION_KEYS = frozenset({"SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "GIT_ASKPASS",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "NODE_OPTIONS", "NODE_PATH",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH"})
MODEL_PREFIXES = ("OPENAI_", "OPENROUTER_", "ANTHROPIC_", "QWEN_", "DASHSCOPE_",
                  "GEMINI_", "GOOGLE_API_", "AZURE_OPENAI_", "MISTRAL_", "COHERE_")
MAVEN_INJECTION = frozenset({"MAVEN_ARGS", "MAVEN_OPTS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS",
                            "_JAVA_OPTIONS", "JAVA_OPTS", "MAVEN_DEBUG_OPTS", "MAVEN_BASEDIR",
                            "MAVEN_PROJECTBASEDIR", "MAVEN_CONFIG"})


def inherited_env(environ: Mapping[str, str] | None = None, *, maven: bool = False) -> dict[str, str]:
    source = os.environ if environ is None else environ
    denied = INJECTION_KEYS | (MAVEN_INJECTION if maven else frozenset())
    prefixes = SERVICE_PREFIXES + (MODEL_PREFIXES if maven else ())
    result = {key: value for key, value in source.items()
              if key not in denied and not key.startswith(prefixes)}
    if maven:
        # The launcher sources system/user mavenrc before reading these variables.
        # An rc file could otherwise restore stripped MAVEN_ARGS or JVM options.
        result["MAVEN_SKIP_RC"] = "1"
    return result
