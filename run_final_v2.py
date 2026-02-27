import asyncio
import json
import os
import time
import requests
from datetime import datetime

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY
from dotenv import load_dotenv

# Carrega variáveis do .env
load_dotenv()

# ==========================================
# CONFIGURAÇÃO AVANÇADA (VIA .ENV OU DEFAULTS)
# ==========================================

PRIVATE_KEY = os.getenv("POLY_KEY")
HOST = "https://clob.polymarket.com"             # SEM barra final
CHAIN_ID = 137

# Estratégia
BANKROLL        = float(os.getenv("POLY_BANKROLL",      "20.0"))   # Banca total
STAKE_PCT       = float(os.getenv("POLY_STAKE_PCT",     "0.10"))   # 10% por trade
MIN_PROFIT      = float(os.getenv("POLY_MIN_PROFIT",    "0.005"))  # 0.5% lucro mínimo
MAX_SPREAD_COST = float(os.getenv("POLY_MAX_COST",      "1.00"))   # Break even máximo

# Execução
SCAN_INTERVAL = int(os.getenv("POLY_SCAN_INTERVAL", "1"))          # Intervalo em segundos
DRY_RUN = os.getenv("POLY_DRY_RUN", "true").lower() == "true"      # Simulação por padrão

# Filtros de Mercado
MIN_LIQUIDITY = float(os.getenv("POLY_MIN_LIQ", "5.0"))            # Liquidez mínima ($5)

# Headers para evitar bloqueio da API
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ArbBot/2.1)",
    "Accept": "application/json"
}


class SpreadArbBot:

    def __init__(self):
        self.bankroll = BANKROLL
        self.trades = 0
        self.total_profit = 0.0
        self.opportunities_seen = 0
        self.start_time = time.time()
        self.client = None

        print("\n" + "="*40)
        print("🤖 POLYMARKET ARBITRAGE BOT v2.1 (OTIMIZADO)")
        print("="*40)
        print(f"💰 Bankroll: ${self.bankroll:.2f}")
        print(f"🎯 Meta Lucro: {MIN_PROFIT*100:.2f}%")
        print(f"📦 Stake por trade: {STAKE_PCT*100:.1f}%")
        print(f"⚡ Intervalo Scan: {SCAN_INTERVAL}s")
        print(f"🧪 Modo Simulação (DRY_RUN): {DRY_RUN}")
        print("="*40 + "\n")

        if not PRIVATE_KEY:
            print("[ERRO CRÍTICO] Variável POLY_KEY não definida!")
            exit(1)

        try:
            self.client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
            print("[SISTEMA] Conectado à Polygon/CLOB com sucesso! 🟢\n")
        except Exception as e:
            print(f"[ERRO FATAL] Falha na conexão: {e}")
            exit(1)

    # ==============================
    # BUSCAR MERCADOS
    # ==============================
    def get_markets(self):
        """Busca mercados da CLOB com campos reais da API (snake_case)."""
        try:
            url = "https://clob.polymarket.com/markets"
            r = requests.get(url, timeout=4)
            print(f"[GET_MARKETS] HTTP {r.status_code}")
            if r.status_code != 200:
                print(f"[API] Erro {r.status_code} ao buscar mercados. Body: {r.text[:200]}")
                return []
            data = r.json()
            # Normalização da resposta
            if isinstance(data, dict):
                raw = data.get("data", [])
                print(f"[GET_MARKETS] dict com chave 'data' → {len(raw)} itens")
            elif isinstance(data, list):
                raw = data
                print(f"[GET_MARKETS] lista simples → {len(raw)} itens")
            else:
                print(f"[GET_MARKETS] Formato inesperado: {type(data)}")
                return []
            valid_markets = []
            for m in raw:
                if not isinstance(m, dict):
                    continue
                # A API usa snake_case:
                #  - enable_order_book (não enableOrderBook)
                #  - provavelmente clob_token_ids (não clobTokenIds)
                is_active = m.get("active", False)
                has_order_book = m.get("enable_order_book", False)
                # clob_token_ids pode vir como lista ou string JSON
                clob_ids = m.get("clob_token_ids") or m.get("clobTokenIds")
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except json.JSONDecodeError:
                        clob_ids = None
                if not is_active:
                    continue
                if not has_order_book:
                    continue
                if not clob_ids:
                    continue
                # Garante pelo menos 2 tokens (YES/NO)
                if isinstance(clob_ids, list) and len(clob_ids) < 2:
                    continue
                valid_markets.append(m)
            print(f"[GET_MARKETS] Pós-filtro: {len(valid_markets)} mercados válidos")
            return valid_markets
        except Exception as e:
            print(f"[ERRO GET_MARKETS] {e}")
            return []

    # ==============================
    # VERIFICAR OPORTUNIDADE
    # ==============================
    def check_opportunity(self, market):
        """Analisa o orderbook e calcula o spread/custo de arbitragem."""
        try:
            ids = market.get("clobTokenIds")
            if isinstance(ids, str):
                ids = json.loads(ids)

            if not isinstance(ids, list) or len(ids) < 2:
                return None

            t_yes, t_no = ids[0], ids[1]

            url_yes = f"{HOST}/book?token_id={t_yes}"
            url_no  = f"{HOST}/book?token_id={t_no}"

            with requests.Session() as s:
                r1 = s.get(url_yes, headers=HEADERS, timeout=2).json()
                r2 = s.get(url_no,  headers=HEADERS, timeout=2).json()

            if not r1.get("asks") or not r2.get("asks"):
                return None

            yes_price = float(r1["asks"][0]["price"])
            yes_size  = float(r1["asks"][0]["size"])

            no_price  = float(r2["asks"][0]["price"])
            no_size   = float(r2["asks"][0]["size"])

            total_cost = yes_price + no_price

            # Liquidez disponível em $
            liq_yes_usd   = yes_size * yes_price
            liq_no_usd    = no_size  * no_price
            max_trade_usd = min(liq_yes_usd, liq_no_usd)

            if max_trade_usd < MIN_LIQUIDITY:
                return None

            return {
                "slug":      market.get("slug", "unknown"),
                "yes_price": yes_price,
                "no_price":  no_price,
                "cost":      total_cost,
                "max_trade": max_trade_usd,
                "t_yes":     t_yes,
                "t_no":      t_no
            }

        except Exception:
            return None

    # ==============================
    # EXECUTAR TRADE
    # ==============================
    def execute_trade(self, opp):
        """Executa a arbitragem (ou simula)."""
        cost          = opp["cost"]
        profit_margin = 1.0 - cost

        # Stake respeitando liquidez disponível
        target_stake     = self.bankroll * STAKE_PCT
        real_stake       = min(target_stake, opp["max_trade"])
        projected_profit = real_stake * profit_margin

        # Log de oportunidade (mesmo abaixo do mínimo, para monitorar)
        if cost < 1.0:
            status = "✅ ENTRADA" if profit_margin >= MIN_PROFIT else "⚠️ SPREAD BAIXO"
            print(
                f"[{status}] {opp['slug'][:40]} | "
                f"Custo: {cost:.4f} | Margem: {profit_margin*100:.2f}% | "
                f"Stake: ${real_stake:.2f} | Liq: ${opp['max_trade']:.2f}"
            )

        # Critério de entrada
        if profit_margin < MIN_PROFIT:
            return

        if DRY_RUN:
            self.trades += 1
            self.total_profit += projected_profit
            print(
                f"   [SIMULAÇÃO] 🚀 Ordem enviada! "
                f"Lucro est.: ${projected_profit:.4f} | "
                f"Total Acumulado: ${self.total_profit:.4f}"
            )
            return

        # EXECUÇÃO REAL
        try:
            print("   [REAL] 🚀 Enviando ordens para CLOB...")

            o1 = self.client.create_and_post_order(
                OrderArgs(
                    price=opp["yes_price"],
                    size=round(real_stake / opp["yes_price"], 4),
                    side=BUY,
                    token_id=opp["t_yes"]
                )
            )
            o2 = self.client.create_and_post_order(
                OrderArgs(
                    price=opp["no_price"],
                    size=round(real_stake / opp["no_price"], 4),
                    side=BUY,
                    token_id=opp["t_no"]
                )
            )

            print(f"   [SUCESSO] Ordens criadas! IDs: {o1} | {o2}")
            self.trades += 1
            self.total_profit += projected_profit
            self.bankroll += projected_profit

            print(
                f"   [STATS] Trades: {self.trades} | "
                f"Lucro acumulado: ${self.total_profit:.4f} | "
                f"Bankroll: ${self.bankroll:.2f}"
            )

        except Exception as e:
            print(f"   [FALHA EXECUÇÃO] {e}")

    # ==============================
    # LOOP PRINCIPAL
    # ==============================
    async def run(self):
        print("[MONITOR] Iniciando loop de varredura...")

        try:
            while True:
                start_scan = time.time()
                markets = self.get_markets()

                best_cost = 2.0
                best_slug = ""
                opportunities = 0

                for m in markets:
                    opp = self.check_opportunity(m)
                    if opp:
                        if opp["cost"] < best_cost:
                            best_cost = opp["cost"]
                            best_slug = opp["slug"]

                        self.execute_trade(opp)

                        if opp["cost"] < 1.0:
                            opportunities += 1

                duration = time.time() - start_scan
                msg_best = (
                    f"Melhor: {best_cost:.4f} ({best_slug[:15]}...)"
                    if best_cost < 2.0 else "Nenhum spread < 1.0"
                )

                print(
                    f"[SCAN] {datetime.utcnow().strftime('%H:%M:%S')} | "
                    f"Mkts: {len(markets)} | Custo < 1.0: {opportunities} | "
                    f"{msg_best} | Tempo: {duration:.2f}s"
                )

                await asyncio.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("\n[ENCERRADO] Bot parado pelo usuário.")
            print(
                f"[RESUMO] Trades: {self.trades} | "
                f"Lucro: ${self.total_profit:.4f} | "
                f"Bankroll final: ${self.bankroll:.2f}"
            )


# ==============================
# MAIN
# ==============================
if __name__ == "__main__":
    bot = SpreadArbBot()
    asyncio.run(bot.run())
