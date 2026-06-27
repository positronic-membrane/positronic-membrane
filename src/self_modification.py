import logging
import re

logger = logging.getLogger("JanusSelfModification")

_DISABLED = "Direct source modification is disabled. Use the skill staging harness or a Project Sandbox."


def stage_and_test(rel_path, proposed_code):
    raise PermissionError(_DISABLED)


def stage_and_test_multi(modifications):
    raise PermissionError(_DISABLED)


def apply_staged_change(temp_dir_path, rel_path):
    raise PermissionError(_DISABLED)


def apply_staged_multi(temp_dir_path, modifications):
    raise PermissionError(_DISABLED)


def generate_diff(rel_path, proposed_code):
    raise PermissionError(_DISABLED)


def generate_multi_diff(modifications):
    raise PermissionError(_DISABLED)


def apply_search_replace_blocks(current_content: str, block_text: str) -> str:
    """
    Parses search/replace blocks from 'block_text' and applies them to 'current_content'.
    Format:
    <<<<<<< SEARCH
    [original content]
    =======
    [replacement content]
    >>>>>>> REPLACE
    """
    pattern = r"<<<<<<< SEARCH\r?\n(.*?)\r?\n=======\r?\n(.*?)\r?\n>>>>>>> REPLACE"
    blocks = re.findall(pattern, block_text, re.DOTALL)

    if not blocks:
        raise ValueError("Invalid search/replace block syntax. No blocks could be parsed.")

    updated_content = current_content
    for search_part, replace_part in blocks:
        search_norm = search_part.replace("\r\n", "\n")
        content_norm = updated_content.replace("\r\n", "\n")

        count = updated_content.count(search_part)
        if count == 0:
            count_norm = content_norm.count(search_norm)
            if count_norm == 0:
                raise ValueError(f"Search block not found in the target content:\n{search_part}")
            elif count_norm > 1:
                raise ValueError(
                    f"Search block matches multiple times ({count_norm}) when normalized. "
                    f"Please make it more specific:\n{search_part}"
                )
            else:
                parts = content_norm.split(search_norm, 1)
                updated_content = parts[0] + replace_part + parts[1]
        elif count > 1:
            raise ValueError(
                f"Search block matches multiple times ({count}) in the file. "
                f"Please make it more specific:\n{search_part}"
            )
        else:
            updated_content = updated_content.replace(search_part, replace_part, 1)

    return updated_content
