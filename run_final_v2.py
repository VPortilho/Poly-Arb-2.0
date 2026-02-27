import asyncio
import json
import os
import requests
from datetime import datetime
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY
from dotenv import load_dotenv

# Carrega variaveis do arquivo .env se existir
load_dotenv()

# CONFIGURACAO
PRIVATE_KEY = os.getenv("POLY_KEY")
HOST = "https://clob.polymarket.com/"
CHAIN_ID = 137
MIN_SPREAD_PROFIT = 0.02      # 2% de lucro alvo
SCAN_INTERVAL = 3             # segundos
DRY_RUN = True               # se quiser so simular, mude para True

class SpreadArbBot:
    def __init__(self):
        self.bankroll = 100.0
        self.trades = 0
        self.profit = 0.0
        self.client = None
        print("[FINAL BOT] Iniciando...")

        if not PRIVATE_KEY:
            print("[ERRO] Variavel POLY_KEY nao definida no .env ou ambiente.")
            exit(1)

        try:
            self.client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
            print("[LIVE] Carteira conectada! 🟢")
        except Exception as e:
            print(f"[ERRO CONEXAO] {e}")
            exit(1)

        def get_markets(self):
        """
        Busca mercados diretamente da CLOB, ja com clobTokenIds.
        """
        try:
            url = "https://clob.polymarket.com/markets"
            r = requests.get(url, timeout=5)
            data = r.json()

            # DEBUG basico pra ver o formato da resposta
            if isinstance(data, dict):
                print("[DEBUG API] Resposta dict, chaves:", list(data.keys())[:5])
                return []
            if not isinstance(data, list):
                print("[DEBUG API] Tipo inesperado:", type(data), str(data)[:200])
                return []

            markets = []
            for m in data:
                if not isinstance(m, dict):
                    continue  # ignora strings ou outros tipos
                if (
                    m.get("active")
                    and m.get("enableOrderBook")
                    and m.get("clobTokenIds")
                ):
                    markets.append(m)

            return markets

        except Exception as e:
            print(f"[ERRO API] {e}")
            return []

    def check_spread(self, market):
        try:
            # clobTokenIds vem como string JSON
            clob_ids = market.get("clobTokenIds", "[]")
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)

            if len(clob_ids) < 2:
                return None

            t_yes = clob_ids[0]
            t_no = clob_ids[1]

            # Orderbook de cada lado
            r_yes = requests.get(
                f"https://clob.polymarket.com/book?token_id={t_yes}",
                timeout=2
            ).json()
            r_no = requests.get(
                f"https://clob.polymarket.com/book?token_id={t_no}",
                timeout=2
            ).json()

            if not r_yes.get("asks") or not r_no.get("asks"):
                return None

            yes_ask = float(r_yes["asks"][0]["price"])
            no_ask = float(r_no["asks"][0]["price"])

            cost = yes_ask + no_ask
            if cost >= (1.00 - MIN_SPREAD_PROFIT):
                return None

            current_stake = self.bankroll * 0.10

            yes_vol = float(r_yes["asks"][0]["size"]) * yes_ask
            no_vol = float(r_no["asks"][0]["size"]) * no_ask
            max_liquidity = min(yes_vol, no_vol)

            if max_liquidity < current_stake:
                return None

            profit_pct = (1 - cost) * 100
            print(
                f"[OPORTUNIDADE] {market.get('slug', 'sem-slug')} | "
                f"Lucro: {profit_pct:.2f}% | Stake: ${current_stake:.2f}"
            )
            return yes_ask, no_ask, current_stake, t_yes, t_no
        except Exception as e:
            print(f"[ERRO CHECK-SPREAD] {e}")
            return None

    def execute(self, m_slug, yes_p, no_p, stake, t_yes, t_no):
        print(f"[EXEC] 🚀 {m_slug}")
        
        if DRY_RUN:
            print(f"[SIMULACAO] Ordem nao enviada (DRY_RUN=True). Stake: ${stake:.2f}")
            return

        try:
            o1 = self.client.create_and_post_order(
                OrderArgs(price=yes_p, size=stake / yes_p, side=BUY, token_id=t_yes)
            )
            print(f" ✅ YES: {o1}")
            o2 = self.client.create_and_post_order(
                OrderArgs(price=no_p, size=stake / no_p, side=BUY, token_id=t_no)
            )
            print(f" ✅ NO: {o2}")
            self.trades += 1
        except Exception as e:
            print(f"[ERRO EXEC] {e}")

    async def run(self):
        print(f"[STATUS] Monitorando oportunidades... (Intervalo: {SCAN_INTERVAL}s)")
        while True:
            mkts = self.get_markets()
            # print(f"[{datetime.now().strftime('%H:%M:%S')}] Scan {len(mkts)}...") # Descomente para log a cada scan
            for m in mkts:
                opp = self.check_spread(m)
                if opp:
                    self.execute(m.get("slug", "sem-slug"), *opp)
            await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    bot = SpreadArbBot()
    asyncio.run(bot.run())
