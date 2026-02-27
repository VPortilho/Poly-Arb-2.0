import asyncio
import json
import os
import time
from datetime import datetime, UTC

import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY

# =========================
# CONFIG / ENV
# =========================

load_dotenv()

PRIVATE_KEY = os.getenv("POLY_KEY")
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

# Configuração via .env (com defaults seguros)
BANKROLL      = float(os.getenv("POLY_BANKROLL",     "20.0"))
STAKE_PCT     = float(os.getenv("POLY_STAKE_PCT",    "0.10"))       # 10% da banca por trade
MIN_PROFIT    = float(os.getenv("POLY_MIN_PROFIT",   "0.005"))      # 0.5% de edge mínimo
MIN_LIQUIDITY = float(os.getenv("POLY_MIN_LIQUIDITY","5.0"))        # liquidez mínima em USD
SCAN_INTERVAL = int(os.getenv("POLY_SCAN_INTERVAL",  "3"))          # segundos
DRY_RUN       = os.getenv("POLY_DRY_RUN", "true").lower() == "true" # simulação por padrão


class SpreadArbBot:
    def __init__(self):
        self.bankroll     = BANKROLL
        self.trades       = 0
        self.total_profit = 0.0
        self.client       = None

        print("=" * 50)
        print("🤖 POLYMARKET ARB BOT - FÓRMULA SIMPLES")
        print("=" * 50)
        print(f"Bankroll: ${self.bankroll:.2f}")
        print(f"Stake por trade: {STAKE_PCT*100:.1f}% da banca")
        print(f"Lucro mínimo (edge): {MIN_PROFIT*100:.2f}%")
        print(f"Liquidez mínima: ${MIN_LIQUIDITY:.2f}")
        print(f"Intervalo de scan: {SCAN_INTERVAL}s")
        print(f"DRY_RUN (simulação): {DRY_RUN}")
        print("=" * 50)

        if not PRIVATE_KEY:
            print("[ERRO] POLY_KEY não definido no .env ou ambiente.")
            raise SystemExit(1)

        try:
            self.client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
            print("[LIVE] Conectado à carteira na Polygon 🟢")
        except Exception as e:
            print(f"[ERRO CONEXAO] {e}")
            raise SystemExit(1)

    # =========================
    # BUSCAR MERCADOS
    # =========================
    def get_markets(self):
        """
        Busca mercados da CLOB.
        Forma esperada da API:
        {
          "data": [ { ... mercado ... }, ... ],
          "next_cursor": ...,
          ...
        }
        """
        try:
            url = f"{HOST}/markets"
            r = requests.get(url, timeout=5)
            status = r.status_code
            if status != 200:
                print(f"[GET_MARKETS] HTTP {status} - corpo: {r.text[:200]}")
                return []

            data = r.json()
            if isinstance(data, dict):
                raw = data.get("data", [])
            elif isinstance(data, list):
                raw = data
            else:
                print(f"[GET_MARKETS] Formato inesperado: {type(data)}")
                return []

            valid = []
            for m in raw:
                if not isinstance(m, dict):
                    continue

                # Campos reais da API: active, enable_order_book, clob_token_ids
                if not m.get("active", False):
                    continue

                if not m.get("enable_order_book", False):
                    continue

                clob_ids = m.get("clob_token_ids")
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except Exception:
                        clob_ids = None

                if not clob_ids or not isinstance(clob_ids, list):
                    continue
                if len(clob_ids) < 2:
                    continue

                valid.append(m)

            print(
                f"[GET_MARKETS] Total bruto: {len(raw)} | "
                f"após filtros: {len(valid)}"
            )
            return valid

        except Exception as e:
            print(f"[ERRO GET_MARKETS] {e}")
            return []

    # =========================
    # CHECAR OPORTUNIDADE
    # =========================
    def check_opportunity(self, market):
        """
        Fórmula simples:
        - Pega melhor ask de YES e NO.
        - Custo = yes_ask + no_ask
        - Edge = 1 - custo
        - Se edge >= MIN_PROFIT e liquidez >= stake → oportunidade.
        """
        try:
            clob_ids = market.get("clob_token_ids")
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    return None

            if not isinstance(clob_ids, list) or len(clob_ids) < 2:
                return None

            t_yes, t_no = clob_ids[0], clob_ids[1]

            url_yes = f"{HOST}/book?token_id={t_yes}"
            url_no  = f"{HOST}/book?token_id={t_no}"

            with requests.Session() as s:
                r1 = s.get(url_yes, timeout=3).json()
                r2 = s.get(url_no,  timeout=3).json()

            if not r1.get("asks") or not r2.get("asks"):
                return None

            yes_ask  = float(r1["asks"][0]["price"])
            yes_size = float(r1["asks"][0]["size"])
            no_ask   = float(r2["asks"][0]["price"])
            no_size  = float(r2["asks"][0]["size"])

            total_cost = yes_ask + no_ask
            edge       = 1.0 - total_cost  # ex: 0.01 = 1%

            # Liquidez disponível em USD
            liq_yes_usd  = yes_size * yes_ask
            liq_no_usd   = no_size  * no_ask
            max_liquidity = min(liq_yes_usd, liq_no_usd)

            if edge <= 0:
                return None

            slug = market.get("slug") or market.get("question") or "sem-slug"

            return {
                "slug":         slug,
                "yes_ask":      yes_ask,
                "no_ask":       no_ask,
                "cost":         total_cost,
                "edge":         edge,
                "max_liquidity":max_liquidity,
                "t_yes":        t_yes,
                "t_no":         t_no,
            }

        except Exception:
            return None

    # =========================
    # EXECUTAR (OU SIMULAR) TRADE
    # =========================
    def execute_trade(self, opp):
        cost = opp["cost"]
        edge = opp["edge"]
        slug = opp["slug"]

        target_stake = self.bankroll * STAKE_PCT
        stake        = min(target_stake, opp["max_liquidity"])

        if stake <= 0:
            return

        projected_profit = stake * edge

        # Log sempre que encontrar edge positiva
        status = (
            "✅ ENTRADA"
            if edge >= MIN_PROFIT and stake >= MIN_LIQUIDITY
            else "⚠️ EDGE BAIXO"
        )
        print(
            f"[{status}] {slug[:60]} | "
            f"custo={cost:.4f} | edge={edge*100:.3f}% | "
            f"stake=${stake:.2f} | liq=${opp['max_liquidity']:.2f}"
        )

        # Critério de entrada real
        if edge < MIN_PROFIT:
            return
        if stake < MIN_LIQUIDITY:
            return

        if DRY_RUN:
            self.trades       += 1
            self.total_profit += projected_profit
            print(
                f"   [SIMULAÇÃO] lucro est.: ${projected_profit:.4f} | "
                f"PnL simulado: ${self.total_profit:.4f}"
            )
            return

        # Execução real na CLOB
        try:
            print("   [REAL] Enviando ordens para CLOB...")
            o1 = self.client.create_and_post_order(
                OrderArgs(
                    price=opp["yes_ask"],
                    size=round(stake / opp["yes_ask"], 4),
                    side=BUY,
                    token_id=opp["t_yes"],
                )
            )
            o2 = self.client.create_and_post_order(
                OrderArgs(
                    price=opp["no_ask"],
                    size=round(stake / opp["no_ask"], 4),
                    side=BUY,
                    token_id=opp["t_no"],
                )
            )
            print(f"   [SUCESSO] Ordens criadas: YES={o1} | NO={o2}")
            self.trades += 1
        except Exception as e:
            print(f"   [ERRO EXEC] {e}")

    # =========================
    # LOOP PRINCIPAL
    # =========================
    async def run(self):
        print("[STATUS] Iniciando loop de varredura...\n")
        while True:
            t0      = time.time()
            markets = self.get_markets()

            best_edge = -1.0
            best_slug = None

            for m in markets:
                opp = self.check_opportunity(m)
                if not opp:
                    continue

                if opp["edge"] > best_edge:
                    best_edge = opp["edge"]
                    best_slug = opp["slug"]

                self.execute_trade(opp)

            dt  = time.time() - t0
            now = datetime.now(UTC).strftime("%H:%M:%S")

            if best_edge > 0:
                msg_best = f"melhor edge={best_edge*100:.3f}% ({best_slug[:40]})"
            else:
                msg_best = "nenhum edge > 0"

            print(
                f"[SCAN] {now} | mkts={len(markets)} | {msg_best} | tempo={dt:.2f}s\n"
            )

            await asyncio.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    bot = SpreadArbBot()
    asyncio.run(bot.run())
