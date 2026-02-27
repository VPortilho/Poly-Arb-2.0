import asyncio
import json
import os
import requests
from datetime import datetime
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY
from dotenv import load_dotenv

# Carrega variáveis do .env
load_dotenv()

# CONFIGURAÇÃO
PRIVATE_KEY = os.getenv("POLY_KEY")
HOST = "https://clob.polymarket.com"   # FIX 1: removida barra final
CHAIN_ID = 137
MIN_SPREAD_PROFIT = 0.005              # 0.5%
SCAN_INTERVAL = 3                      # segundos
DRY_RUN = True                         # False = envia ordens reais

# FIX 2: headers para evitar bloqueio da API
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ArbBot/1.0)",
    "Accept": "application/json"
}


class SpreadArbBot:

    def __init__(self):
        self.bankroll = 20.0
        self.trades = 0
        self.profit = 0.0
        self.client = None

        print("[FINAL BOT] Iniciando...")

        if not PRIVATE_KEY:
            print("[ERRO] Variável POLY_KEY não definida.")
            exit(1)

        try:
            self.client = ClobClient(
                host=HOST,
                key=PRIVATE_KEY,
                chain_id=CHAIN_ID
            )
            print("[LIVE] Carteira conectada 🟢")
        except Exception as e:
            print(f"[ERRO CONEXAO] {e}")
            exit(1)

    # ==============================
    # BUSCAR MERCADOS
    # ==============================
    def get_markets(self):
        try:
            url = "https://clob.polymarket.com/markets"
            r = requests.get(url, timeout=5)
            data = r.json()

            print(f"[DEBUG] Tipo da resposta: {type(data)}")
            if isinstance(data, dict):
                print("[DEBUG] Chaves do dict:", list(data.keys())[:10])

            markets = []

            # Resposta padrão: dict com chave "data" que contém a lista de mercados
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                raw_markets = data["data"]
            # fallback: se vier direto como lista
            elif isinstance(data, list):
                raw_markets = data
            else:
                print("[DEBUG] Formato inesperado de resposta:", type(data))
                return []

            print(f"[DEBUG] Total bruto de mercados: {len(raw_markets)}")

            for m in raw_markets:
                if not isinstance(m, dict):
                    continue
                # filtros básicos
                if not m.get("active"):
                    continue
                if not m.get("enableOrderBook"):
                    continue
                if not m.get("clobTokenIds"):
                    continue
                markets.append(m)

            print(f"[DEBUG] Mercados após filtros: {len(markets)}")
            return markets

        except Exception as e:
            print(f"[ERRO API] {e}")
            return []

    # ==============================
    # VERIFICAR SPREAD
    # ==============================
    def check_spread(self, market):
        try:
            clob_ids = market.get("clobTokenIds", "[]")

            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)

            if len(clob_ids) < 2:
                return None

            t_yes = clob_ids[0]
            t_no = clob_ids[1]

            r_yes = requests.get(
                f"{HOST}/book?token_id={t_yes}",
                headers=HEADERS,
                timeout=2
            ).json()

            r_no = requests.get(
                f"{HOST}/book?token_id={t_no}",
                headers=HEADERS,
                timeout=2
            ).json()

            if not r_yes.get("asks") or not r_no.get("asks"):
                return None

            yes_ask = float(r_yes["asks"][0]["price"])
            no_ask = float(r_no["asks"][0]["price"])

            cost = yes_ask + no_ask

            if cost >= (1 - MIN_SPREAD_PROFIT):
                return None

            # FIX 4: stake dividido entre os dois lados (não dobrar o risco)
            total_stake = self.bankroll * 0.10
            yes_stake = total_stake * yes_ask / cost
            no_stake = total_stake * no_ask / cost

            yes_vol = float(r_yes["asks"][0]["size"]) * yes_ask
            no_vol = float(r_no["asks"][0]["size"]) * no_ask
            max_liquidity = min(yes_vol, no_vol)

            if max_liquidity < total_stake:
                return None

            profit_pct = (1 - cost) * 100

            print(
                f"[OPORTUNIDADE] {market.get('slug', 'sem-slug')} | "
                f"Lucro: {profit_pct:.2f}% | Stake Total: ${total_stake:.2f}"
            )

            return yes_ask, no_ask, yes_stake, no_stake, t_yes, t_no

        except Exception as e:
            print(f"[ERRO CHECK-SPREAD] {e}")
            return None

    # ==============================
    # EXECUTAR ORDENS
    # ==============================
    def execute(self, slug, yes_p, no_p, yes_stake, no_stake, t_yes, t_no):
        print(f"[EXEC] 🚀 {slug}")

        # FIX 5: calcula lucro esperado e registra
        total_invested = yes_stake + no_stake
        expected_profit = (1 - (yes_p + no_p)) * total_invested

        if DRY_RUN:
            print(
                f"[SIMULAÇÃO] Ordem não enviada (DRY_RUN=True) | "
                f"Investido: ${total_invested:.2f} | Lucro esperado: ${expected_profit:.4f}"
            )
            return

        try:
            o1 = self.client.create_and_post_order(
                OrderArgs(
                    price=yes_p,
                    size=round(yes_stake / yes_p, 4),  # FIX 6: arredondar size
                    side=BUY,
                    token_id=t_yes
                )
            )
            print(f"✅ YES: {o1}")

            o2 = self.client.create_and_post_order(
                OrderArgs(
                    price=no_p,
                    size=round(no_stake / no_p, 4),    # FIX 6: arredondar size
                    side=BUY,
                    token_id=t_no
                )
            )
            print(f"✅ NO: {o2}")

            # FIX 5: atualiza bankroll e métricas
            self.trades += 1
            self.profit += expected_profit
            self.bankroll += expected_profit
            print(
                f"[STATS] Trades: {self.trades} | "
                f"Lucro acumulado: ${self.profit:.4f} | "
                f"Bankroll: ${self.bankroll:.2f}"
            )

        except Exception as e:
            print(f"[ERRO EXEC] {e}")

    # ==============================
    # LOOP PRINCIPAL
    # ==============================
    async def run(self):
        print(f"[STATUS] Monitorando oportunidades... (intervalo: {SCAN_INTERVAL}s)")
        print(f"[STATUS] Bankroll inicial: ${self.bankroll:.2f}")

        try:
            while True:
                markets = self.get_markets()
                print(f"[SCAN] {datetime.now().strftime('%H:%M:%S')} — {len(markets)} mercados ativos")

                for m in markets:
                    opp = self.check_spread(m)
                    if opp:
                        self.execute(m.get("slug", "sem-slug"), *opp)

                await asyncio.sleep(SCAN_INTERVAL)

        # FIX 7: encerramento limpo com Ctrl+C
        except KeyboardInterrupt:
            print("\n[ENCERRADO] Bot parado pelo usuário.")
            print(
                f"[RESUMO] Trades: {self.trades} | "
                f"Lucro: ${self.profit:.4f} | "
                f"Bankroll final: ${self.bankroll:.2f}"
            )


# ==============================
# MAIN
# ==============================
if __name__ == "__main__":
    bot = SpreadArbBot()
    asyncio.run(bot.run())
