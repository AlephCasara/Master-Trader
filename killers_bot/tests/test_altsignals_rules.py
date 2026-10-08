"""Parser deterministico do canal AltSignals (killers_bot/altsignals_rules.py).

O que precisa ser provado:

- cada variante do cabecalho do sinal vira um `open` com moeda base, direcao,
  entrada, SL e alvos; nunca None, sempre o schema do classificador
- o guarda de typo (SL ausente/longe/do lado errado, alvos do lado errado)
  rebaixa para `chat` com o motivo
- o repost do mesmo sinal em ate 900 s vira `chat` (duplicate_of), mas a edicao
  da propria mensagem nao e marcada como duplicata
- avisos de fechamento viram `close_full` com pct assinado; fill, TP parcial,
  `close` solto e texto qualquer viram `chat`
- a allowlist da Hyperliquid e opcional, em cache, e falha ABERTA
- a linha `TARGETS:` anexada para o receiver e lida pelo extrator do receiver

Todas as mensagens sao sinteticas: imitam o formato, com moedas e precos
inventados.
"""
import importlib
import importlib.util
import io
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from killers_bot import altsignals_rules as rules  # noqa: E402
from killers_bot import observer  # noqa: E402

SCHEMA = (ROOT / "killers_bot" / "schema.sql").read_text()
T0 = datetime(2026, 10, 2, 8, 17, 15, tzinfo=timezone.utc)

SCHEMA_KEYS = {"id", "kind", "signal_id", "symbol", "direction", "entry",
               "entry_range", "sl", "tp", "pct", "applies_to", "confidence",
               "notes"}

LONG_ETH = ("Coin: ETHUSDT Long\nLeverage: Isolated x10\nEntry: 2500.5\n"
            "Target 1: 2550\nTarget 2: 2600\nSL: 2450")
SHORT_ETH = ("Coin: ETHUSDT Short\nLeverage: Isolated x10\nEntry: 2500.5\n"
             "Target 1: 2450\nTarget 2: 2400\nSL: 2550")


@pytest.fixture(autouse=True)
def universo_limpo(monkeypatch):
    monkeypatch.delenv(rules.HL_INFO_URL_ENV, raising=False)
    rules._universe_cache.update(url=None, at=0.0, names=None, failed_at=0.0)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    yield c
    c.close()


def _gravar(conn, msg_id, text, date):
    """Faz o que o observer faz: persiste o raw e a classificacao."""
    observer.persist_raw(conn, {"id": msg_id, "text": text, "date": date})
    cls = rules.classify_altsignals(text, msg_id, conn, date)
    observer.persist_classification(conn, cls)
    return cls


# ── Sinal de abertura ──────────────────────────────────────────────────────


@pytest.mark.parametrize("texto,simbolo,direcao,entrada,sl,alvos", [
    (LONG_ETH, "ETH", "long", 2500.5, 2450.0, [2550.0, 2600.0]),
    (SHORT_ETH, "ETH", "short", 2500.5, 2550.0, [2450.0, 2400.0]),
    ("Coin: $SOLUSDT Long\nLeverage: Isolated x5\nEntry:: 140\n"
     "Target 1: 145\nTarget 2: 150\nSL:: 135", "SOL", "long", 140.0, 135.0,
     [145.0, 150.0]),
    ("DOGEUSDT SHORT\nLeverage: ISolated x20\nEntry: 0.1500\n"
     "Target 1: 0.1450\nTarget 2: 0.1400\nSL: 0.1550", "DOGE", "short", 0.15,
     0.155, [0.145, 0.14]),
    ("ADAUSDT Long\nLeverage: Isolated x3\nEntry: 0.55\nTarget 1: 0.57\n"
     "Stoploss: 0.53", "ADA", "long", 0.55, 0.53, [0.57]),
    ("XRPUSDT Short \nLeverage: Cross x10\nEntry:: 2.40\nTarget 1: 2.30\n"
     "Target 2: 2.20\nSL: 2.50", "XRP", "short", 2.4, 2.5, [2.3, 2.2]),
])
def test_variantes_do_cabecalho_viram_open(texto, simbolo, direcao, entrada, sl, alvos):
    cls = rules.classify_altsignals(texto, 42)
    assert set(cls) >= SCHEMA_KEYS
    assert cls["id"] == 42
    assert cls["kind"] == "open"
    assert cls["signal_id"] is None
    assert cls["symbol"] == simbolo
    assert cls["direction"] == direcao
    assert cls["entry"] == entrada
    assert cls["entry_range"] == [entrada, entrada]
    assert cls["sl"] == sl
    assert cls["targets"] == alvos
    assert cls["confidence"] == 1.0
    assert "targets=" in cls["notes"]


def test_alavancagem_aparece_nas_notas():
    assert "x10" in rules.classify_altsignals(LONG_ETH, 1)["notes"]


# ── Guarda de typo ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("texto,motivo", [
    ("Coin: ETHUSDT Long\nLeverage: Isolated x10\nEntry: 2500\n"
     "Target 1: 2550\nSL: 1800", "more than"),
    ("Coin: ETHUSDT Short\nLeverage: Isolated x10\nEntry: 2500\n"
     "Target 1: 2450\nSL: 3300", "more than"),
    ("Coin: ETHUSDT Long\nLeverage: Isolated x10\nEntry: 2500\n"
     "Target 1: 2550\nTarget 2: 2400\nSL: 2450", "targets"),
    ("Coin: ETHUSDT Short\nLeverage: Isolated x10\nEntry: 2500\n"
     "Target 1: 2550\nSL: 2550", "targets"),
    ("Coin: ETHUSDT Long\nLeverage: Isolated x10\nEntry: 2500\n"
     "Target 1: 2550\nTarget 2: 2600", "no SL"),
    ("Coin: ETHUSDT Long\nLeverage: Isolated x10\nEntry: 2500\n"
     "Target 1: 2550\nSL: 2560", "wrong side"),
])
def test_guarda_de_typo_rebaixa_para_chat(texto, motivo):
    cls = rules.classify_altsignals(texto, 7)
    assert cls["kind"] == "chat"
    assert cls["notes"].startswith("typo_guard")
    assert motivo in cls["notes"]


def test_sl_a_exatamente_25_por_cento_ainda_abre():
    texto = ("Coin: ETHUSDT Long\nLeverage: Isolated x2\nEntry: 100\n"
             "Target 1: 110\nSL: 75")
    assert rules.classify_altsignals(texto, 1)["kind"] == "open"


# ── Duplicata ──────────────────────────────────────────────────────────────


def test_repost_dentro_de_900s_vira_duplicata(conn):
    assert _gravar(conn, 10, LONG_ETH, T0)["kind"] == "open"
    repost = _gravar(conn, 11, LONG_ETH, T0 + timedelta(seconds=1))
    assert repost["kind"] == "chat"
    assert repost["notes"] == "duplicate_of=10"


def test_repost_fora_da_janela_abre_de_novo(conn):
    _gravar(conn, 10, LONG_ETH, T0)
    dentro = rules.classify_altsignals(LONG_ETH, 11, conn, T0 + timedelta(seconds=899))
    fora = rules.classify_altsignals(LONG_ETH, 11, conn, T0 + timedelta(seconds=901))
    assert dentro["kind"] == "chat"
    assert fora["kind"] == "open"


def test_edicao_da_mesma_mensagem_nao_e_duplicata(conn):
    _gravar(conn, 10, LONG_ETH, T0)
    editada = rules.classify_altsignals(LONG_ETH, 10, conn, T0 + timedelta(seconds=30))
    assert editada["kind"] == "open"


def test_data_vinda_do_raw_json_como_texto_tambem_conta(conn):
    _gravar(conn, 10, LONG_ETH, T0)
    repost = rules.classify_altsignals(
        LONG_ETH, 11, conn, str(T0 + timedelta(seconds=5)))
    assert repost["notes"] == "duplicate_of=10"


@pytest.mark.parametrize("outro", [
    SHORT_ETH,
    LONG_ETH.replace("Entry: 2500.5", "Entry: 2499"),
    LONG_ETH.replace("SL: 2450", "SL: 2440"),
    LONG_ETH.replace("ETHUSDT", "BTCUSDT"),
])
def test_sinal_diferente_nao_e_duplicata(conn, outro):
    _gravar(conn, 10, LONG_ETH, T0)
    assert rules.classify_altsignals(outro, 11, conn, T0 + timedelta(seconds=1))["kind"] == "open"


def test_chat_nao_serve_de_original_para_duplicata(conn):
    ruim = LONG_ETH.replace("SL: 2450", "SL: 1")
    assert _gravar(conn, 10, ruim, T0)["kind"] == "chat"
    assert rules.classify_altsignals(LONG_ETH, 11, conn, T0)["kind"] == "open"


# ── Avisos de fechamento ───────────────────────────────────────────────────


@pytest.mark.parametrize("texto,simbolo,pct", [
    ("#APT/USDT Stop Target Hit ⛔\nLoss: 38.961% \U0001f4c9", "APT", -38.961),
    ("#SOL/USDT All targets achieved \U0001f60e\nProfit: 40.2% \U0001f4c8\n"
     "Period: 3 hr 27 min ⏰", "SOL", 40.2),
    ("#STX/USDT Manually Cancelled", "STX", None),
    ("#STX/USDT Manually Cancelled\nProfit: 6.44%\U0001f4c8\n"
     "Period: 4 hours 12 min ⏰", "STX", 6.44),
    ("#STX/USDT Manually Cancelled\nLoss: 3.5% \U0001f4c9", "STX", -3.5),
    ("Exchange A, Exchange B, Exchange C\n"
     "#CHZ/USDT Closed due to opposite direction signal ⚠", "CHZ", None),
    ("#BTC/USDT Closed at trailing stoploss after reaching take profit ⚠",
     "BTC", None),
    ("#BTC/USDT Closed at trailing stoploss after reaching take profit ⚠\n"
     "Profit: 12.5% \U0001f4c8", "BTC", 12.5),
])
def test_avisos_de_fechamento_viram_close_full(texto, simbolo, pct):
    cls = rules.classify_altsignals(texto, 5)
    assert set(cls) >= SCHEMA_KEYS
    assert cls["kind"] == "close_full"
    assert cls["symbol"] == simbolo
    assert cls["signal_id"] is None
    assert cls["pct"] == pct


# ── O que nunca abre nem fecha ─────────────────────────────────────────────


@pytest.mark.parametrize("texto,nota", [
    ("#BTC/USDT All entries achieved\nAverage Entry Price: 86000 \U0001f4b5",
     "fill_confirm"),
    ("#JASMY/USDT Take-Profit target 1 ✅\nProfit: 6.7% \U0001f4c8\n"
     "Period: 13 hr 17 min ⏰", "tp_hit"),
    ("close", "bare close"),
    ("Close", "bare close"),
    ("Oh lord make it rain", "unparsed"),
    ("\U0001f6a8 MACRO WARNING\n\nYields are up, stay cautious.", "unparsed"),
    ("Test", "unparsed"),
    ("", "empty"),
    (None, "empty"),
    ("#BTC/USDT something nobody has seen before", "unparsed notice"),
    ("Coin: ETHUSDT Long\nEntry: 2500", "unparsed"),
])
def test_o_resto_vira_chat_e_nunca_none(texto, nota):
    cls = rules.classify_altsignals(texto, 9)
    assert cls is not None
    assert set(cls) >= SCHEMA_KEYS
    assert cls["kind"] == "chat"
    assert nota in cls["notes"]


def test_fill_confirm_guarda_a_entrada_media_nas_notas():
    cls = rules.classify_altsignals(
        "#BTC/USDT All entries achieved\nAverage Entry Price: 86000", 9)
    assert "86000" in cls["notes"]
    assert cls["symbol"] == "BTC"


# ── Allowlist da Hyperliquid ───────────────────────────────────────────────


@pytest.fixture()
def universo(monkeypatch):
    monkeypatch.setenv(rules.HL_INFO_URL_ENV, "http://hl-info.invalid/info")
    chamadas = []

    def fake(url):
        chamadas.append(url)
        return {"ETH", "BTC", "kPEPE"}

    monkeypatch.setattr(rules, "_fetch_universe", fake)
    return chamadas


def test_moeda_fora_do_universo_vira_chat(universo):
    texto = LONG_ETH.replace("ETHUSDT", "ZZZUSDT")
    cls = rules.classify_altsignals(texto, 3)
    assert cls["kind"] == "chat"
    assert cls["notes"] == "not_on_hyperliquid"
    assert cls["symbol"] == "ZZZ"


def test_moeda_no_universo_continua_open(universo):
    assert rules.classify_altsignals(LONG_ETH, 3)["kind"] == "open"


def test_nome_com_prefixo_k_da_hyperliquid_e_aceito(universo):
    texto = LONG_ETH.replace("ETHUSDT", "PEPEUSDT")
    assert rules.classify_altsignals(texto, 3)["kind"] == "open"


def test_universo_fica_em_cache(universo):
    for i in range(3):
        rules.classify_altsignals(LONG_ETH, i)
    assert len(universo) == 1


def test_universo_expirado_e_buscado_de_novo(universo, monkeypatch):
    rules.classify_altsignals(LONG_ETH, 1)
    rules._universe_cache["at"] -= rules.HL_CACHE_SEC + 1
    rules.classify_altsignals(LONG_ETH, 2)
    assert len(universo) == 2


def test_erro_na_hyperliquid_falha_aberto(monkeypatch):
    monkeypatch.setenv(rules.HL_INFO_URL_ENV, "http://hl-info.invalid/info")
    chamadas = []

    def quebra(url):
        chamadas.append(url)
        raise OSError("connection refused")

    monkeypatch.setattr(rules, "_fetch_universe", quebra)
    assert rules.classify_altsignals(LONG_ETH, 1)["kind"] == "open"
    assert rules.classify_altsignals(LONG_ETH, 2)["kind"] == "open"
    assert len(chamadas) == 1, "falha recente nao deve bloquear cada mensagem"


def test_url_com_esquema_estranho_falha_aberto(monkeypatch):
    monkeypatch.setenv(rules.HL_INFO_URL_ENV, "file:///etc/passwd")
    monkeypatch.setattr(rules, "_fetch_universe",
                        lambda url: pytest.fail("nao deveria buscar"))
    assert rules.classify_altsignals(LONG_ETH, 1)["kind"] == "open"


def test_sem_variavel_de_ambiente_nao_consulta_nada(monkeypatch):
    monkeypatch.setattr(rules, "_fetch_universe",
                        lambda url: pytest.fail("nao deveria buscar"))
    assert rules.classify_altsignals(LONG_ETH, 1)["kind"] == "open"


def test_fetch_universe_faz_post_de_meta_e_le_os_nomes(monkeypatch):
    vistos = {}

    class Resposta(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        vistos["method"] = req.get_method()
        vistos["body"] = json.loads(req.data)
        vistos["timeout"] = timeout
        return Resposta(json.dumps(
            {"universe": [{"name": "BTC"}, {"name": "kPEPE"}]}).encode())

    monkeypatch.setattr(rules.urllib.request, "urlopen", fake_urlopen)
    assert rules._fetch_universe("http://hl-info.invalid/info") == {"BTC", "kPEPE"}
    assert vistos == {"method": "POST", "body": {"type": "meta"},
                      "timeout": rules.HL_TIMEOUT_SEC}


# ── Linha TARGETS para o receiver ──────────────────────────────────────────


_receiver_cache = {}


def _extrator_do_receiver():
    if "fn" not in _receiver_cache:
        pytest.importorskip("fastapi")
        pytest.importorskip("aiohttp")
        pkg_dir = ROOT / "services" / "killers-receiver" / "app"
        spec = importlib.util.spec_from_file_location(
            "killers_receiver_app", pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["killers_receiver_app"] = pkg
        spec.loader.exec_module(pkg)
        main = importlib.import_module("killers_receiver_app.main")
        _receiver_cache["fn"] = main.extract_targets_from_text
    return _receiver_cache["fn"]


def test_formato_exato_da_linha_targets():
    assert observer._targets_line([2550.0, 2600.0]) == "TARGETS: 2550.0 - 2600.0"
    assert observer._targets_line([0.0000123, 0.0000119]) == \
        "TARGETS: 0.0000123 - 0.0000119"
    assert observer._targets_line([0.145]) == "TARGETS: 0.145"


@pytest.mark.parametrize("texto", [
    LONG_ETH,
    SHORT_ETH,
    "DOGEUSDT SHORT\nLeverage: ISolated x20\nEntry: 0.1500\nTarget 1: 0.0000145\n"
    "Target 2: 0.0000140\nSL: 0.1550",
])
def test_linha_targets_anexada_e_lida_pelo_receiver(texto):
    extrair = _extrator_do_receiver()
    msg = {"id": 1, "text": texto, "date": T0}
    cls = rules.classify_altsignals(texto, 1)
    enviada = observer._receiver_msg(msg, cls)
    assert extrair(enviada["text"]) == cls["targets"]
    assert extrair(texto) == [], "o texto cru nao tem linha TARGETS"
    assert msg["text"] == texto, "a mensagem guardada nao muda"
    assert enviada["text"].startswith(texto + "\nTARGETS: ")


def test_receiver_msg_so_mexe_em_open_com_alvos():
    msg = {"id": 1, "text": "x"}
    assert observer._receiver_msg(msg, {"kind": "chat", "targets": [1.0]}) is msg
    assert observer._receiver_msg(msg, {"kind": "open"}) is msg
    assert observer._receiver_msg(msg, {"kind": "open", "targets": []}) is msg
