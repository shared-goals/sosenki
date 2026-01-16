#!/usr/bin/env python3
"""
Translation completeness validation script for SOSenki.

Validates that translations.json contains all required translation keys used in:
1. Bot handlers - t("flat_key") pattern in Python files
2. Mini App - t("flat_key") pattern in JavaScript files
3. HTML - data-i18n="flat_key" attributes

Single source of truth: src/static/mini_app/translations.json
Flat structure with prefixes: btn_, prompt_, msg_, err_, status_, empty_, nav_, hint_, title_, label_, action_

Provides warnings for:
- Missing keys in translations.json
- Unused keys in translations.json (defined but not used in code)

Usage:
    uv run python scripts/check_translations.py
    make check-i18n
"""

import json
import re
import sys
from pathlib import Path


def load_translations(file_path: Path) -> dict:
    """Load translation JSON file."""
    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"❌ Error loading {file_path}: {e}")
        sys.exit(1)


def extract_keys_from_code(code: str) -> set:
    """Extract all translation keys from Python/JavaScript code using t("key") pattern."""
    # Pattern: t("key") or t('key') - matches ANY key (flat or dotted)
    # Use word boundary \b before 't' to avoid matching .get() or other methods
    # Use DOTALL flag to handle multiline function calls
    pattern = r"\bt\(\s*['\"]([a-z_.]+)['\"]\s*[,\)]"
    matches = re.findall(pattern, code, re.DOTALL)
    return set(matches)


def extract_keys_from_html(code: str) -> set:
    """Extract all translation keys from HTML using data-i18n attributes."""
    # Pattern: data-i18n="key" or data-i18n-html="key" - matches ANY key (flat or dotted)
    pattern = r'data-i18n(?:-html)?=["\']([a-z_.]+)["\']'
    matches = re.findall(pattern, code)
    return set(matches)


def find_hardcoded_russian_text(code: str, file_path: str) -> list:  # noqa: C901
    """Find hardcoded Russian/Cyrillic text that should use translations.

    Returns list of (line_num, snippet) tuples for hardcoded Russian text outside comments.
    """
    issues = []
    cyrillic_pattern = r"[а-яёА-ЯЁ]"
    in_multiline_string = False
    in_js_comment = False

    for line_num, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()

        # Track Python multiline docstrings
        if '"""' in stripped or "'''" in stripped:
            # Count occurrences - odd means toggle state
            if stripped.count('"""') % 2 == 1 or stripped.count("'''") % 2 == 1:
                in_multiline_string = not in_multiline_string
            continue

        if in_multiline_string:
            continue

        # Track JS multiline comments (/* ... */)
        if "/*" in stripped:
            in_js_comment = True
        if "*/" in stripped:
            in_js_comment = False
            continue
        if in_js_comment:
            continue

        # Skip single-line comments
        if stripped.startswith("#") or stripped.startswith("//"):
            continue

        # Skip JSDoc/block comment lines (lines starting with *)
        if stripped.startswith("*"):
            continue

        # Skip HTML comments
        if "<!--" in stripped:
            continue

        # Check for Cyrillic text
        if re.search(cyrillic_pattern, line):
            # Skip lines that properly use t() function or data-i18n
            if "data-i18n" in line:
                continue

            # If line has t() call, it's likely using translations properly
            # BUT we should still catch strings that are NOT inside t()
            # For now, flag any line with Cyrillic for manual review
            issues.append((line_num, stripped[:100]))

    return issues


def check_translations():  # noqa: C901
    """Main validation function."""
    project_root = Path(__file__).parent.parent

    # Single source of truth
    ru_json_path = project_root / "src" / "static" / "mini_app" / "translations.json"

    # Python files that use t("flat_key")
    python_files = list((project_root / "src" / "bot" / "handlers").glob("*.py"))
    python_files.extend(
        [
            project_root / "src" / "services" / "notification_service.py",
            project_root / "src" / "api" / "mini_app.py",
        ]
    )

    # JavaScript files that use t("flat_key")
    js_files = [
        project_root / "src" / "static" / "mini_app" / "app.js",
    ]

    # HTML files that use data-i18n attributes
    html_files = [
        project_root / "src" / "static" / "mini_app" / "index.html",
    ]

    # Load translations from single backend file
    translations = load_translations(ru_json_path)
    available_keys = set(translations.keys())

    print("\n🔍 Translation Validation Report\n")
    print("=" * 60)
    print(f"📍 Single source of truth: {ru_json_path.relative_to(project_root)}")
    print(
        "📚 Flat structure with prefixes: btn_, prompt_, msg_, err_, status_, empty_, hint_, title_, label_"
    )
    print("=" * 60)

    # Extract keys from all Python files
    python_keys = set()
    for py_file in python_files:
        if py_file.exists():
            with open(py_file, encoding="utf-8") as f:
                code = f.read()
                python_keys.update(extract_keys_from_code(code))

    # Extract keys from JavaScript files
    js_keys = set()
    for js_file in js_files:
        if js_file.exists():
            with open(js_file, encoding="utf-8") as f:
                code = f.read()
                js_keys.update(extract_keys_from_code(code))

    # Extract keys from HTML files
    html_keys = set()
    for html_file in html_files:
        if html_file.exists():
            with open(html_file, encoding="utf-8") as f:
                code = f.read()
                html_keys.update(extract_keys_from_html(code))

    # Combine all used keys
    used_keys = python_keys | js_keys | html_keys

    print(f"\n📋 Found {len(python_keys)} keys used in Python handlers")
    print(f"📋 Found {len(js_keys)} keys used in JavaScript")
    print(f"📋 Found {len(html_keys)} keys used in HTML")
    print(f"📚 Found {len(available_keys)} total keys defined in translations.json\n")

    # Check for hardcoded Russian text
    print("🔍 Checking for hardcoded Russian text...\n")
    all_hardcoded = []

    for py_file in python_files:
        if py_file.exists():
            with open(py_file, encoding="utf-8") as f:
                code = f.read()
                hardcoded = find_hardcoded_russian_text(code, str(py_file))
                for line_num, snippet in hardcoded:
                    all_hardcoded.append((py_file.name, line_num, snippet))

    for js_file in js_files:
        if js_file.exists():
            with open(js_file, encoding="utf-8") as f:
                code = f.read()
                hardcoded = find_hardcoded_russian_text(code, str(js_file))
                for line_num, snippet in hardcoded:
                    all_hardcoded.append((js_file.name, line_num, snippet))

    for html_file in html_files:
        if html_file.exists():
            with open(html_file, encoding="utf-8") as f:
                code = f.read()
                hardcoded = find_hardcoded_russian_text(code, str(html_file))
                for line_num, snippet in hardcoded:
                    all_hardcoded.append((html_file.name, line_num, snippet))

    if all_hardcoded:
        print(
            f"⚠️  Found {len(all_hardcoded)} hardcoded Russian text(s) (should use translations.json):"
        )
        for filename, line_num, snippet in all_hardcoded:
            print(f"   {filename}:{line_num} → {snippet}")
        print()

    # Check for missing keys
    missing_keys = used_keys - available_keys
    if missing_keys:
        print(f"⚠️  Missing translations ({len(missing_keys)}):")
        for key in sorted(missing_keys):
            print(f"   - {key}")
        print()

    # Check for unused keys
    unused_keys = available_keys - used_keys
    if unused_keys:
        print(f"ℹ️  Unused translations ({len(unused_keys)}) defined but not used:")
        for key in sorted(unused_keys):
            print(f"   - {key}")
        print()

    # Summary
    print("=" * 60)
    if not missing_keys:
        print("✅ All translation keys are properly defined!\n")
        return 0
    else:
        print(f"❌ {len(missing_keys)} missing translation key(s)\n")
        return 1


if __name__ == "__main__":
    sys.exit(check_translations())
