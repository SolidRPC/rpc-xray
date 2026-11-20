import { CHAINS } from "./contract.js";

export function chainLabel(chainId: number): string {
  return CHAINS[String(chainId)] ?? `eip155:${chainId}`;
}
