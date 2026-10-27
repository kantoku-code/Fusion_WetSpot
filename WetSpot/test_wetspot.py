"""Fusion を起動せずにアドインのライフサイクルを検証する。"""

import ast
import contextlib
import importlib
import json
from pathlib import Path
from string import Formatter
import sys
import types
import unittest
from unittest import mock


class _AdskModule(types.ModuleType):
    def __getattr__(self, name):
        value = type(name, (), {})
        setattr(self, name, value)
        return value


if "adsk" not in sys.modules:
    adsk = _AdskModule("adsk")
    adsk.core = _AdskModule("adsk.core")
    adsk.fusion = _AdskModule("adsk.fusion")
    sys.modules.update(
        {"adsk": adsk, "adsk.core": adsk.core, "adsk.fusion": adsk.fusion}
    )

_HERE = Path(__file__).resolve().parent
sys.path[:] = [path for path in sys.path if Path(path or ".").resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))
addin = importlib.import_module("WetSpot.WetSpot")
i18n = importlib.import_module("WetSpot.i18n")


class _Event:
    def __init__(self):
        self.handlers = []

    def add(self, handler):
        self.handlers.append(handler)


class _Item:
    def __init__(self, owner):
        self.owner = owner
        self.deleted = False

    def deleteMe(self):
        self.deleted = True
        self.owner.item = None


class _Control(_Item):
    isPromoted = False
    isPromotedByDefault = False


class _Controls:
    def __init__(self):
        self.item = None

    def itemById(self, _id):
        return self.item

    def addCommand(self, _definition):
        self.item = _Control(self)
        return self.item


class _Graphics:
    def __init__(self, names):
        self.items = []
        for name in names:
            group = types.SimpleNamespace(name=name)
            group.deleteMe = lambda group=group: self.items.remove(group)
            self.items.append(group)

    @property
    def count(self):
        return len(self.items)

    def item(self, index):
        return self.items[index]


class _Definitions:
    def __init__(self):
        self.item = None

    def itemById(self, _id):
        return self.item

    def addButtonDefinition(self, _id, name, _description):
        self.item = _Item(self)
        self.item.name = name
        self.item.description = _description
        self.item.commandCreated = _Event()
        return self.item


class _CommandInput:
    def __init__(self):
        self.items = []
        self.listItems = types.SimpleNamespace(
            add=lambda *args: self.items.append(args)
        )

    def addSelectionFilter(self, *_):
        pass

    def setSelectionLimits(self, *_):
        pass


class _CommandInputs:
    def __init__(self):
        self.calls = []

    def addSelectionInput(self, *_):
        self.calls.append(_)
        return _CommandInput()

    def addBoolValueInput(self, *_):
        self.calls.append(_)
        return _CommandInput()

    def addDropDownCommandInput(self, *_):
        self.calls.append(_)
        self.mode = _CommandInput()
        return self.mode


def _command():
    command = types.SimpleNamespace(commandInputs=_CommandInputs())
    for name in (
        "activate",
        "inputChanged",
        "executePreview",
        "preSelect",
        "validateInputs",
        "execute",
        "destroy",
    ):
        setattr(command, name, _Event())
    return command


class WetSpotLifecycleTest(unittest.TestCase):
    def setUp(self):
        addin._handlers.clear()
        addin._sessions.clear()
        self.controls = _Controls()
        self.definitions = _Definitions()
        panel = types.SimpleNamespace(controls=self.controls)
        ui = types.SimpleNamespace(
            allToolbarPanels=types.SimpleNamespace(itemById=lambda _id: panel),
            commandDefinitions=self.definitions,
            messageBox=mock.Mock(),
        )
        self.app = types.SimpleNamespace(
            userInterface=ui,
            preferences=types.SimpleNamespace(
                generalPreferences=types.SimpleNamespace(userLanguage=8)
            ),
            activeProduct=object(),
            activeViewport=types.SimpleNamespace(refresh=mock.Mock()),
            log=mock.Mock(),
        )
        self.app_patch = mock.patch.object(
            addin.core.Application, "get", return_value=self.app, create=True
        )
        self.app_patch.start()
        self.style_patch = mock.patch.object(
            addin.core.DropDownStyles, "TextListDropDownStyle", 0, create=True
        )
        self.style_patch.start()
        languages = types.SimpleNamespace(
            GermanLanguage=5,
            EnglishLanguage=3,
            SpanishLanguage=13,
            FrenchLanguage=4,
            ItalianLanguage=7,
            JapaneseLanguage=8,
            KoreanLanguage=9,
            PolishLanguage=10,
            PortugueseBrazilianLanguage=11,
            ChinesePRCLanguage=0,
            TurkishLanguage=14,
        )
        self.languages_patch = mock.patch.object(addin.core, "UserLanguages", languages)
        self.languages_patch.start()

    def tearDown(self):
        self.style_patch.stop()
        self.languages_patch.stop()
        self.app_patch.stop()
        addin._handlers.clear()
        addin._sessions.clear()

    def test_button_stays_available_until_stop_and_can_restart(self):
        addin.run(None)
        definition = self.definitions.item
        control = self.controls.item
        self.assertEqual("WetSpot", definition.name)
        self.assertTrue(control.isPromoted)
        self.assertEqual(1, len(definition.commandCreated.handlers))
        self.assertFalse(hasattr(definition, "execute"))

        addin.run(None)
        self.assertIs(definition, self.definitions.item)
        self.assertEqual(1, len(addin._handlers))

        addin.stop(None)
        self.assertTrue(control.deleted)
        self.assertTrue(definition.deleted)
        self.assertFalse(addin._handlers)

        addin.run(None)
        self.assertIsNot(definition, self.definitions.item)
        self.assertEqual(1, len(addin._handlers))
        addin.stop(None)

    def test_command_destroy_releases_only_its_handlers(self):
        addin.run(None)
        persistent = addin._handlers[0]
        root = object()
        with mock.patch.object(
            addin.fusion.Design,
            "cast",
            return_value=types.SimpleNamespace(rootComponent=root),
            create=True,
        ):
            first_command = _command()
            persistent.notify(types.SimpleNamespace(command=first_command))
            second_command = _command()
            persistent.notify(types.SimpleNamespace(command=second_command))
        self.assertEqual(
            2, len(addin._sessions), self.app.userInterface.messageBox.call_args
        )
        first, second = addin._sessions
        self.assertEqual(7, len(first["handlers"]))
        self.assertEqual(1, len(first_command.destroy.handlers))
        with mock.patch.object(addin, "_clear_preview") as clear:
            first_command.destroy.handlers[0].notify(None)
            clear.assert_called_once_with(root)
        self.assertEqual([second], addin._sessions)
        self.assertFalse(first["handlers"])
        self.assertIs(persistent, addin._handlers[0])
        self.assertIsNotNone(self.controls.item)
        addin.stop(None)

    def test_cancel_removes_preview_and_keeps_button(self):
        addin.run(None)
        graphics = _Graphics([addin._PREVIEW_NAME, "unrelated"])
        root = types.SimpleNamespace(customGraphicsGroups=graphics)
        with mock.patch.object(
            addin.fusion.Design,
            "cast",
            return_value=types.SimpleNamespace(rootComponent=root),
            create=True,
        ):
            command = _command()
            addin._handlers[0].notify(types.SimpleNamespace(command=command))
        command.destroy.handlers[0].notify(None)
        self.assertEqual(["unrelated"], [group.name for group in graphics.items])
        self.assertFalse(addin._sessions)
        self.assertIsNotNone(self.controls.item)
        addin.stop(None)

    def test_stop_clears_preview_for_open_command(self):
        addin.run(None)
        root = object()
        session = {"root": root, "handlers": [object()]}
        addin._sessions.append(session)
        with mock.patch.object(addin, "_clear_preview") as clear:
            addin.stop(None)
            clear.assert_called_once_with(root)
        self.assertFalse(session["handlers"])
        self.assertFalse(addin._sessions)

    def test_stop_retries_preview_cleanup_after_destroy_failure(self):
        addin.run(None)
        root = object()
        session = {"root": root, "handlers": [object()]}
        addin._sessions.append(session)
        with mock.patch.object(
            addin, "_clear_preview", side_effect=[RuntimeError("busy"), None]
        ) as clear:
            addin._DestroyHandler(session).notify(None)
            self.assertEqual([session], addin._sessions)
            self.assertFalse(session["handlers"])
            addin.stop(None)
            self.assertEqual(2, clear.call_count)
        self.assertFalse(addin._sessions)

    def test_fusion_language_controls_button_and_command_inputs(self):
        self.app.preferences.generalPreferences.userLanguage = 5
        addin.run(None)
        self.assertEqual(
            i18n.tr("button_description", "de"), self.definitions.item.description
        )
        command = _command()
        root = object()
        with mock.patch.object(
            addin.fusion.Design,
            "cast",
            return_value=types.SimpleNamespace(rootComponent=root),
            create=True,
        ):
            addin._handlers[0].notify(types.SimpleNamespace(command=command))
        self.assertIn(
            (
                addin._BODY_ID,
                i18n.tr("bodies_label", "de"),
                i18n.tr("bodies_prompt", "de"),
            ),
            command.commandInputs.calls,
        )
        self.assertEqual(
            i18n.tr("mode_accurate", "de"), command.commandInputs.mode.items[0][0]
        )
        addin.stop(None)

        self.app.preferences.generalPreferences.userLanguage = 8
        addin.run(None)
        self.assertEqual(i18n.tr("button_name", "ja"), self.definitions.item.name)
        addin.stop(None)

    def test_locale_fallbacks(self):
        for name, language in i18n._LANGUAGES.items():
            self.app.preferences.generalPreferences.userLanguage = getattr(
                addin.core.UserLanguages, name
            )
            self.assertEqual(language, i18n.current_language())
        self.app.preferences.generalPreferences.userLanguage = 999
        self.assertEqual("en", i18n.current_language())
        with mock.patch.object(
            i18n, "catalog", return_value={"en": {"sample": "English"}, "de": {}}
        ):
            self.assertEqual("English", i18n.tr("sample", "de"))

    def test_execution_result_uses_fusion_language(self):
        self.app.preferences.generalPreferences.userLanguage = 5
        pool = types.SimpleNamespace(volume=2.0, max_depth=3.0, area=4.0)
        inputs = types.SimpleNamespace(
            itemById=lambda key: types.SimpleNamespace(
                selectedItem=types.SimpleNamespace(index=0), value=False
            )
        )
        command = types.SimpleNamespace(commandInputs=inputs)
        cache = types.SimpleNamespace(analyze=mock.Mock(return_value=[pool]))
        patches = [
            mock.patch.object(addin, "_selected_bodies", return_value=[object()]),
            mock.patch.object(addin, "_gravity_vector", return_value=object()),
            mock.patch.object(addin, "_clear_preview"),
            mock.patch.object(addin, "_pool_component", return_value=object()),
            mock.patch.object(addin.pool_engine, "clear_failed_pool_graphics"),
            mock.patch.object(
                addin.pool_engine, "create_pool_bodies", return_value=[object()]
            ),
            mock.patch.object(
                addin.pool_engine, "get_last_created_pools", return_value=[pool]
            ),
            mock.patch.object(addin.pool_engine, "create_drain_points"),
            mock.patch.object(
                addin.pool_engine, "format_last_timings", return_value=[]
            ),
        ]
        with self.subTest("German result"):
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                addin._ExecuteHandler(object(), cache).notify(
                    types.SimpleNamespace(command=command)
                )
        self.app.userInterface.messageBox.assert_called_once()
        message = self.app.userInterface.messageBox.call_args.args[0]
        self.assertIn("Körper erstellt", message)
        self.assertIn("Volumen", message)


class TranslationCatalogTest(unittest.TestCase):
    def test_all_translation_calls_have_catalog_keys(self):
        available = set(i18n.catalog()["en"])
        for name in ("WetSpot.py", "pool_engine.py"):
            tree = ast.parse((_HERE / name).read_text(encoding="utf-8"))
            used = {
                node.args[0].value
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            }
            self.assertFalse(used - available, (name, used - available))

    def test_every_language_has_same_keys_and_format_fields(self):
        messages = i18n.catalog()
        expected = {
            "de",
            "en",
            "es",
            "fr",
            "it",
            "ja",
            "ko",
            "pl",
            "pt-BR",
            "zh-CN",
            "tr",
        }
        self.assertEqual(expected, set(messages))
        keys = set(messages["en"])
        formatter = Formatter()
        for language, translations in messages.items():
            self.assertEqual(keys, set(translations), language)
            for key, template in translations.items():
                fields = [
                    (field, spec)
                    for _, field, spec, _ in formatter.parse(template)
                    if field
                ]
                reference = [
                    (field, spec)
                    for _, field, spec, _ in formatter.parse(messages["en"][key])
                    if field
                ]
                self.assertEqual(sorted(reference), sorted(fields), (language, key))
                values = {
                    name: (
                        1
                        if name in {"count", "index"}
                        else "value"
                        if name in {"details", "reason", "sketch", "stage"}
                        else 1.25
                    )
                    for name, _ in fields
                }
                self.assertIsInstance(i18n.tr(key, language, **values), str)

    def test_manifest_has_descriptions_for_all_languages(self):
        manifest = json.loads((_HERE / "WetSpot.manifest").read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "",
                "1031",
                "1033",
                "1034",
                "1036",
                "1040",
                "1041",
                "1042",
                "1045",
                "1046",
                "2052",
                "1055",
            },
            set(manifest["description"]),
        )


if __name__ == "__main__":
    unittest.main()
