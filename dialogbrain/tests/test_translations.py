"""Ukrainian and Russian translations: every visible string is translated and nothing is left unwrapped."""

import ast
import csv
import json
import os
import re
from collections import Counter

import frappe
import frappe.translate
from frappe.tests.utils import FrappeTestCase

LANGUAGES = ("uk", "ru")
SKIP_DIRS = {"tests", "node_modules", "dist", "__pycache__", "translations"}
PLACEHOLDER = re.compile(r"\{\d+\}")
CYRILLIC = re.compile("[Ѐ-ӿ]")

# Rows whose translation may equal the source: product names only.
SAME_AS_SOURCE = {"DialogBrain"}
# Rows whose translation may hold no Cyrillic letters: none beyond the same-as-source ones.
# Visible-in-Vue exemptions: product names, the AI badge text, single symbols, file-type labels.
VUE_ALLOWED = {"DialogBrain", "ERPNext", "AI", "PDF", "ZIP", "DOC", "XLS", "MP3", "MP4"}
# Strings translated through a variable (``__(variable)``), invisible to literal extraction:
# the channel labels of inbox/utils.js that are words, and the app's role names.
DYNAMIC_VISIBLE = ("Live chat", "Email", "Chat Agent", "Chat Manager")

JS_CALL = re.compile(
	r"""(?<![\w$.])__\(\s*(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|`((?:\\.|[^`\\])*)`)"""
)
TAG = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>""")
STATIC_ATTR = re.compile(r"""(?<![:@\w-])(?:title|placeholder|aria-label|alt)="([^"]*)\"""")


def app_dir():
	return frappe.get_app_path("dialogbrain")


def source_files(*extensions):
	for folder, dirs, files in os.walk(app_dir()):
		dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
		for name in files:
			if name.endswith(extensions):
				yield os.path.join(folder, name)


def python_strings():
	"""Literals of ``_("…")`` / ``frappe._("…")``; ``_(NAME)`` resolves a module-level string constant."""
	found = set()
	for path in source_files(".py"):
		with open(path, encoding="utf-8") as fh:
			tree = ast.parse(fh.read())
		constants = {}
		for node in tree.body:
			if (
				isinstance(node, ast.Assign)
				and len(node.targets) == 1
				and isinstance(node.targets[0], ast.Name)
				and isinstance(node.value, ast.Constant)
				and isinstance(node.value.value, str)
			):
				constants[node.targets[0].id] = node.value.value
		for node in ast.walk(tree):
			if not (isinstance(node, ast.Call) and node.args):
				continue
			func = node.func
			if not (
				(isinstance(func, ast.Name) and func.id == "_")
				or (isinstance(func, ast.Attribute) and func.attr == "_")
			):
				continue
			arg = node.args[0]
			if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
				found.add(arg.value)
			elif isinstance(arg, ast.Name) and arg.id in constants:
				found.add(constants[arg.id])
	return found


def js_strings():
	found = set()
	for path in source_files(".js", ".vue"):
		with open(path, encoding="utf-8") as fh:
			for match in JS_CALL.finditer(fh.read()):
				text = next(g for g in match.groups() if g is not None)
				found.add(text.replace('\\"', '"').replace("\\'", "'").replace("\\n", "\n"))
	return found


def json_strings():
	"""Labels, descriptions and Select options of the app's DocTypes; report, page and workspace titles."""
	found = set()

	def read(pattern_dir):
		for path in source_files(".json"):
			if f"{os.sep}{pattern_dir}{os.sep}" in path:
				with open(path, encoding="utf-8") as fh:
					yield json.load(fh)

	for doc in read("doctype"):
		found.update((doc["name"], doc.get("description")))
		for field in doc["fields"]:
			found.add(field.get("label"))
			if not field.get("hidden"):
				found.add(field.get("description"))
			if field["fieldtype"] == "Select":
				found.update((field.get("options") or "").split("\n"))
	for doc in read("report"):
		found.add(doc["report_name"])
	for doc in read("page"):
		found.add(doc["title"])
	for doc in read("workspace"):
		found.add(doc["label"])
		found.update(shortcut["label"] for shortcut in doc.get("shortcuts", []))
	return {s for s in found if s and re.search(r"[A-Za-z]", s)}


def visible_strings():
	return python_strings() | js_strings() | json_strings() | set(DYNAMIC_VISIBLE)


def read_rows(lang):
	path = os.path.join(app_dir(), "translations", f"{lang}.csv")
	with open(path, encoding="utf-8", newline="") as fh:
		return list(csv.reader(fh))


def vue_literals(template):
	"""Visible Latin text of a Vue template that is not routed through ``__()``."""
	template = re.sub(r"<!--.*?-->", "", template, flags=re.S)
	template = re.sub(r"\{\{.*?\}\}", "", template, flags=re.S)
	found = [m.group(1).strip() for m in STATIC_ATTR.finditer(template)]
	found += [chunk.strip() for chunk in TAG.sub("\x00", template).split("\x00")]
	return [s for s in found if re.search(r"[A-Za-z]", s) and s not in VUE_ALLOWED]


class TestTranslations(FrappeTestCase):
	def test_every_visible_string_has_uk_and_ru(self):
		strings = visible_strings()
		self.assertGreater(len(strings), 300, "the collector found suspiciously few strings")
		for lang in LANGUAGES:
			keys = {row[0] for row in read_rows(lang)}
			self.assertEqual(sorted(strings - keys), [], f"{lang}.csv lacks these strings")
			self.assertEqual(sorted(keys - strings), [], f"{lang}.csv has rows for strings no longer in the app")

	def test_glossary_is_used_verbatim(self):
		glossary = {
			"Chats": ("Чати", "Чаты"),
			"Conversation": ("Розмова", "Диалог"),
			"Take over": ("Перехопити", "Перехватить"),
			"Give back to agent": ("Повернути агенту", "Вернуть агенту"),
			"Unassigned": ("Не призначено", "Не назначено"),
			"Unanswered": ("Без відповіді", "Без ответа"),
			"Draft": ("Чернетка", "Черновик"),
			"Copilot": ("Підказки", "Подсказки"),
			"Autopilot": ("Автовідповідь", "Автоответ"),
			"Off": ("Вимкнено", "Выключено"),
			"Customer": ("Клієнт", "Клиент"),
			"Lead": ("Лід", "Лид"),
			"Reconnect": ("Перепідключити", "Переподключить"),
			"Disconnect": ("Відключити", "Отключить"),
		}
		for index, lang in enumerate(LANGUAGES):
			rows = {row[0]: row[1] for row in read_rows(lang)}
			for source, expected in glossary.items():
				if expected:
					self.assertEqual(rows[source], expected[index], f"{lang}: {source}")

	def test_placeholders_survive_translation(self):
		for lang in LANGUAGES:
			for row in read_rows(lang):
				self.assertEqual(
					Counter(PLACEHOLDER.findall(row[0])),
					Counter(PLACEHOLDER.findall(row[1])),
					f"{lang}: placeholders differ in {row[0]!r}",
				)

	def test_no_empty_or_untranslated_rows(self):
		for lang in LANGUAGES:
			for row in read_rows(lang):
				self.assertEqual(len(row), 3, f"{lang}: bad row {row!r}")
				source, translation, context = row
				self.assertEqual(context, "", f"{lang}: context is unused ({source!r})")
				self.assertTrue(translation.strip(), f"{lang}: empty translation of {source!r}")
				if source in SAME_AS_SOURCE:
					continue
				self.assertNotEqual(translation, source, f"{lang}: untranslated {source!r}")
				self.assertTrue(CYRILLIC.search(translation), f"{lang}: no Cyrillic in {translation!r}")

	def test_csv_has_no_duplicate_sources(self):
		for lang in LANGUAGES:
			sources = [row[0] for row in read_rows(lang)]
			self.assertEqual([s for s, n in Counter(sources).items() if n > 1], [], lang)

	def test_frappe_loads_the_translations(self):
		frappe.translate.clear_cache()  # translations are cached in Redis
		self.assertEqual(frappe._("Take over", lang="uk"), "Перехопити")
		self.assertEqual(frappe._("Take over", lang="ru"), "Перехватить")
		self.assertEqual(frappe._("Give back to agent", lang="uk"), "Повернути агенту")

	def test_report_group_label_follows_the_request_language(self):
		from dialogbrain.dialogbrain.report import group_label

		previous = frappe.local.lang
		try:
			frappe.local.lang = "uk"
			self.assertEqual(group_label("Manager"), "Менеджер")
			self.assertEqual(group_label("Day"), "День")
			frappe.local.lang = "ru"
			self.assertEqual(group_label("Channel"), "Канал")
			self.assertEqual(group_label("Day"), "День")
		finally:
			frappe.local.lang = previous

	def test_no_visible_literal_is_left_unwrapped_in_vue(self):
		for path in source_files(".vue"):
			with open(path, encoding="utf-8") as fh:
				match = re.search(r"<template>(.*)</template>", fh.read(), re.S)
			self.assertEqual(vue_literals(match.group(1)), [], os.path.basename(path))

	def test_the_vue_check_sees_unwrapped_text(self):
		template = (
			'<div title="Hello" :title="__(\'Wrapped\')"><b>Plain text</b>{{ __("x") }}'
			' <span class="a">AI</span><input placeholder="Type"/>'
			'<i v-if="n > 2" class="ok">{{ y }}</i></div>'
		)
		self.assertEqual(vue_literals(template), ["Hello", "Type", "Plain text"])
