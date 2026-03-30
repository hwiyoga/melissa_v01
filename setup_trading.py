"""
One-time setup: derive Polymarket API credentials from your wallet private key.
Run this ONCE, then copy the output into your .env file.

Before running:
  1. Create a wallet (MetaMask recommended) at metamask.io
  2. Add Polygon network to MetaMask:
       Network name: Polygon
       RPC URL:      https://polygon-rpc.com
       Chain ID:     137
       Symbol:       MATIC
  3. Fund wallet with USDC on Polygon:
       - Buy USDC on Coinbase/Kraken and withdraw to Polygon, OR
       - Bridge USDC from Ethereum using https://portal.polygon.technology
  4. Visit https://polymarket.com and connect your wallet
       (This approves the exchange contracts to spend your USDC — required)
  5. Copy your wallet's private key from MetaMask:
       MetaMask → three dots → Account details → Show private key
  6. Paste the private key into POLY_PRIVATE_KEY in your .env file
  7. Run: python3 setup_trading.py
"""

import os
import sys

from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


def main():
    private_key = os.getenv("POLY_PRIVATE_KEY", "")

    if not private_key or private_key == "your_wallet_private_key_here":
        print("\n[ERROR] Set POLY_PRIVATE_KEY in your .env file first.")
        print("  See the instructions at the top of this file.\n")
        sys.exit(1)

    # Normalize key format
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    print("Connecting to Polymarket CLOB...")

    funder = os.getenv("POLY_FUNDER", "")

    try:
        if funder:
            # Magic.link embedded wallet: signer key + proxy (funder) address
            print(f"Magic.link mode — signer: {private_key[:10]}...  funder: {funder}")
            client = ClobClient(
                CLOB_HOST,
                key=private_key,
                chain_id=POLYGON_CHAIN_ID,
                signature_type=1,
                funder=funder,
            )
        else:
            # Standard EOA wallet (MetaMask)
            client = ClobClient(
                CLOB_HOST,
                key=private_key,
                chain_id=POLYGON_CHAIN_ID,
                signature_type=0,
            )

        print("Deriving API credentials from your wallet...")
        creds = client.create_or_derive_api_creds()

        print("\n" + "=" * 60)
        print("  SUCCESS — Add these to your .env file:")
        print("=" * 60)
        print(f"POLY_API_KEY={creds.api_key}")
        print(f"POLY_API_SECRET={creds.api_secret}")
        print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
        print("=" * 60)
        print("\nKeep these secret — they control your trading account.\n")

    except Exception as e:
        print(f"\n[ERROR] {e}")
        print("\nCommon causes:")
        print("  - Private key is wrong or has a typo")
        print("  - POLY_FUNDER address is wrong (should be your Polymarket profile address)")
        print("  - You haven't visited polymarket.com and connected your wallet yet\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
