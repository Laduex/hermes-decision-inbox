export type Outcome = "recommended" | "alternative" | "rejected" | "abstained";

export interface Option {
  option_id: string;
  label: string;
  details: string;
  reason: string;
  is_recommended: number;
  execution: null | Record<string, string>;
}

export interface CardResponse {
  outcome: Outcome;
  selected_option_id: string | null;
  note: string;
}

export interface Card {
  card_id: string;
  version: number;
  title: string;
  summary: string;
  details: string;
  source_profile: string;
  priority: string;
  evidence: Array<{label: string; value: string; url?: string}>;
  execution: null | Record<string, string>;
  options: Option[];
  response: CardResponse | null;
}

export interface Decision {
  decision_id: string;
  title: string;
  source_profile: string;
  priority: string;
  status: string;
  decision_type: string;
  version: number;
  created_at: string;
  cards: Card[];
}
