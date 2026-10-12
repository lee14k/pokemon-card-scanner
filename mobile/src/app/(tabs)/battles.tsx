import { useCallback, useState } from "react";
import { Pressable, ScrollView, TextInput, View } from "react-native";
import { router, useFocusEffect, useLocalSearchParams } from "expo-router";
import { friendlyError, outcomeLabel, shortDate, shortDateTime, usd } from "@shared/format";
import type { Battle, BattleList, SavedPull } from "@shared/types";
import { api } from "@/lib/api";
import { AppHeader } from "@/features/common/AppHeader";
import { RequireTrainer } from "@/features/common/RequireTrainer";
import {
  Badge,
  Body,
  EmptyState,
  HpBar,
  IconBox,
  Panel,
  Pixel,
  PixelButton,
  Row,
  ScreenTitle,
  SectionTitle,
  StatTile,
  colors,
  inputStyle,
  textStyles,
} from "@/ui/kit";
import { Screen } from "@/ui/Screen";

const OUTCOME_TONE = { win: "green", loss: "red", tie: "gold" } as const;

function Arena() {
  const { pull } = useLocalSearchParams<{ pull?: string }>();
  const [data, setData] = useState<BattleList | null>(null);
  const [inbox, setInbox] = useState<Battle[]>([]);
  const [pulls, setPulls] = useState<SavedPull[] | null>(null);
  const [sel, setSel] = useState("");
  const [handle, setHandle] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    api.listBattles().then(setData).catch(() => setData(null));
    api.battleInbox().then(setInbox).catch(() => setInbox([]));
  }, []);

  useFocusEffect(
    useCallback(() => {
      refresh();
      api
        .listPulls()
        .then((ps) => {
          const verified = ps.filter((p) => p.verified);
          setPulls(verified);
          setSel((cur) =>
            pull && verified.some((p) => p.id === pull) ? pull : cur && verified.some((p) => p.id === cur) ? cur : verified[0]?.id ?? ""
          );
        })
        .catch(() => setPulls([]));
    }, [refresh, pull])
  );

  const act = async (fn: () => Promise<Battle>) => {
    setMsg(null);
    setBusy(true);
    try {
      const b = await fn();
      refresh();
      setOpen(b.id); // pop the result open
    } catch (e) {
      setMsg(friendlyError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={{ gap: 16 }}>
      <Row gap={10}>
        <StatTile value={data?.wins ?? "–"} label="Wins" tone="green" />
        <StatTile value={data?.losses ?? "–"} label="Losses" tone="danger" />
        <StatTile value={data?.ties ?? "–"} label="Ties" />
      </Row>

      {inbox.length > 0 && (
        <>
          <SectionTitle>Challengers</SectionTitle>
          {inbox.map((b) => (
            <Panel key={b.id} tone="flagged">
              <View style={{ gap: 10 }}>
                <Row gap={12}>
                  <IconBox name="swords" tone="red" size={38} />
                  <Body style={{ flex: 1 }}>
                    <Body weight="black">{b.opponent.label}</Body> wants to battle!
                  </Body>
                </Row>
                <Row gap={8}>
                  <PixelButton title="Accept" variant="primary" size="sm" disabled={!sel || busy} onPress={() => act(() => api.acceptBattle(b.id, sel))} />
                  <PixelButton title="Run away" variant="ghost" size="sm" disabled={busy} onPress={() => act(() => api.declineBattle(b.id))} />
                </Row>
              </View>
            </Panel>
          ))}
        </>
      )}

      <SectionTitle>New battle</SectionTitle>
      {pulls && pulls.length === 0 ? (
        <EmptyState
          icon="swords"
          title="No verified packs yet"
          body="Scan a pack with its code card to enter the arena."
          action={<PixelButton title="Scan a pack" variant="primary" onPress={() => router.push("/scan/photo")} />}
        />
      ) : (
        <Panel>
          <View style={{ gap: 12 }}>
            <Body weight="black" size={14}>
              Your pack
            </Body>
            <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8 }}>
              {(pulls ?? []).map((p) => {
                const on = p.id === sel;
                return (
                  <Pressable
                    key={p.id}
                    onPress={() => setSel(p.id)}
                    accessibilityRole="radio"
                    accessibilityState={{ selected: on }}
                    style={{
                      padding: 10,
                      borderRadius: 8,
                      borderWidth: 3,
                      borderColor: on ? colors.gold : colors.ink,
                      backgroundColor: on ? "rgba(255,210,63,0.12)" : colors.surface,
                      gap: 4,
                      minWidth: 120,
                    }}
                  >
                    <Body weight="black" size={13}>
                      {shortDate(p.created_at)}
                    </Body>
                    <Body size={12} tone="muted">
                      {p.cards.length} cards
                    </Body>
                    {p.estimated_value != null && <Body style={textStyles.price}>≈{usd(p.estimated_value)}</Body>}
                  </Pressable>
                );
              })}
            </ScrollView>
            <Row gap={10}>
              <PixelButton title="Random" icon="star" variant="primary" style={{ flex: 1 }} disabled={!sel || busy} onPress={() => act(() => api.randomBattle(sel))} />
              <PixelButton title="Bot" icon="heart" style={{ flex: 1 }} disabled={!sel || busy} onPress={() => act(() => api.botBattle(sel))} />
            </Row>
            <Row gap={10}>
              <TextInput
                style={[inputStyle, { flex: 1 }]}
                placeholder="friend's handle"
                placeholderTextColor={colors.textMuted}
                autoCapitalize="none"
                autoCorrect={false}
                value={handle}
                onChangeText={setHandle}
              />
              <PixelButton title="Challenge" variant="gold" disabled={!sel || !handle.trim() || busy} onPress={() => act(() => api.friendBattle(sel, handle.trim()))} />
            </Row>
            {msg && (
              <Body weight="black" tone="danger">
                {msg}
              </Body>
            )}
          </View>
        </Panel>
      )}

      <SectionTitle>History</SectionTitle>
      {(!data || data.battles.length === 0) && <Body tone="muted">No battles yet — go pick a fight!</Body>}
      {(data?.battles ?? []).map((b) => (
        <BattleCard key={b.id} b={b} open={open === b.id} onToggle={() => setOpen(open === b.id ? null : b.id)} />
      ))}
    </View>
  );
}

function BattleCard({ b, open, onToggle }: { b: Battle; open: boolean; onToggle: () => void }) {
  const mine = b.me.score ?? 0;
  const theirs = b.opponent.score ?? 0;
  const max = Math.max(mine, theirs, 0.01);
  const tone = OUTCOME_TONE[b.outcome as keyof typeof OUTCOME_TONE] ?? "outline";
  return (
    <Pressable onPress={onToggle} accessibilityRole="button" accessibilityState={{ expanded: open }}>
      <Panel padding={12}>
        <View style={{ gap: 10 }}>
          <Row gap={8}>
            <Badge label={outcomeLabel(b.outcome)} tone={tone} />
            <Body size={12} tone="muted" style={{ flex: 1 }}>
              {b.mode} · {shortDateTime(b.created_at)}
            </Body>
            <Body size={14} tone="muted">{open ? "▼" : "▶"}</Body>
          </Row>
          <Row gap={8}>
            <View style={{ flex: 1, gap: 4 }}>
              <Body weight="black">You</Body>
              <HpBar value={Math.round((mine / max) * 100)} max={100} label="" showValue={false} color={colors.green} />
              <Pixel size={9} tone="gold">
                {b.me.score != null ? usd(b.me.score) : "?"}
              </Pixel>
            </View>
            <Pixel size={12} style={{ color: colors.primary }} shadow>
              VS
            </Pixel>
            <View style={{ flex: 1, gap: 4, alignItems: "flex-end" }}>
              <Body weight="black" numberOfLines={1}>
                {b.opponent.label}
              </Body>
              <View style={{ alignSelf: "stretch" }}>
                <HpBar value={Math.round((theirs / max) * 100)} max={100} label="" showValue={false} color={colors.cyan} flip />
              </View>
              <Pixel size={9} tone="gold">
                {b.opponent.score != null ? usd(b.opponent.score) : "?"}
              </Pixel>
            </View>
          </Row>
          {open && (
            <Row gap={12} style={{ alignItems: "flex-start" }}>
              {[b.me, b.opponent].map((side, i) => (
                <View key={i} style={{ flex: 1, gap: 4 }}>
                  <Body size={11} weight="black" tone="muted">
                    {(i === 0 ? "Your pack" : side.label).toUpperCase()}
                  </Body>
                  {side.cards.length === 0 && (
                    <Body size={12} tone="muted">
                      No cards yet
                    </Body>
                  )}
                  {side.cards.map((c, j) => (
                    <Row key={j} style={{ justifyContent: "space-between", borderBottomWidth: 2, borderBottomColor: colors.surface, paddingVertical: 3 }}>
                      <Body size={12} numberOfLines={1} style={{ flex: 1 }}>
                        {c.name ?? "?"}
                      </Body>
                      <Body style={textStyles.price}>{c.price != null ? usd(c.price) : "—"}</Body>
                    </Row>
                  ))}
                </View>
              ))}
            </Row>
          )}
        </View>
      </Panel>
    </Pressable>
  );
}

export default function BattlesTab() {
  return (
    <Screen>
      <AppHeader />
      <ScreenTitle kicker="Arena" title="Pack battles" sub="Highest total pack value wins. Only verified packs can enter." />
      <RequireTrainer icon="swords" reason="Log in to battle other trainers.">
        <Arena />
      </RequireTrainer>
    </Screen>
  );
}
