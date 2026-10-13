import { useCallback, useMemo, useState } from "react";
import { Alert, Platform, TextInput, View, useWindowDimensions } from "react-native";
import { router, useFocusEffect } from "expo-router";
import { compactUsd, friendlyError, priceRange, shortDate } from "@shared/format";
import type { CollectionOut } from "@shared/types";
import { api } from "@/lib/api";
import { AppHeader } from "@/features/common/AppHeader";
import { RequireTrainer } from "@/features/common/RequireTrainer";
import { Badge, Body, CardArt, EmptyState, ErrorBox, LoadingBlock, Panel, PixelButton, Row, ScreenTitle, StatTile, colors, inputStyle, textStyles } from "@/ui/kit";
import { Screen } from "@/ui/Screen";

function confirmRemove(): Promise<boolean> {
  if (Platform.OS === "web") return Promise.resolve(window.confirm("Remove this card from your collection?"));
  return new Promise((resolve) =>
    Alert.alert("Remove card?", "It comes out of your collection.", [
      { text: "Keep it", style: "cancel", onPress: () => resolve(false) },
      { text: "Remove", style: "destructive", onPress: () => resolve(true) },
    ])
  );
}

function Vault() {
  const { width } = useWindowDimensions();
  const [data, setData] = useState<CollectionOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await api.getCollection());
    } catch (e) {
      setError(friendlyError(e));
    }
  }, []);
  useFocusEffect(
    useCallback(() => {
      load();
    }, [load])
  );

  // Optimistic edits, rolled back (and reloaded) if the server says no.
  const changeQty = async (id: string, qty: number) => {
    if (!data || qty < 1) return;
    const prev = data;
    setData({ ...data, cards: data.cards.map((c) => (c.id === id ? { ...c, qty } : c)) });
    setBusy(id);
    try {
      await api.patchCollectionQty(id, qty);
    } catch {
      setData(prev);
      await load();
    } finally {
      setBusy(null);
    }
  };
  const remove = async (id: string) => {
    if (!data || !(await confirmRemove())) return;
    const prev = data;
    setData({ ...data, cards: data.cards.filter((c) => c.id !== id) });
    setBusy(id);
    try {
      await api.deleteCollectionCard(id);
    } catch {
      setData(prev);
      await load();
    } finally {
      setBusy(null);
    }
  };

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const cards = data?.cards ?? [];
    return q ? cards.filter((c) => [c.name, c.set_name, c.card_number].some((f) => f?.toLowerCase().includes(q))) : cards;
  }, [data, query]);

  if (error) return <ErrorBox message={error} onRetry={load} />;
  if (!data) return <LoadingBlock label="Opening your collection…" />;
  if (data.cards.length === 0)
    return (
      <EmptyState
        icon="chest"
        title="Your vault is empty"
        body="Snap a binder page and every card on it lands here."
        action={<PixelButton title="Scan a binder page" variant="primary" onPress={() => router.push("/scan/binder")} />}
      />
    );

  const totalQty = data.cards.reduce((n, c) => n + c.qty, 0);
  const cols = width > 600 ? 4 : 2;
  const tileW = (width - 32 - 12 * (cols - 1)) / cols;

  return (
    <View style={{ gap: 14 }}>
      <Row gap={10}>
        <StatTile value={data.cards.length} label="Unique" />
        <StatTile value={totalQty} label="Total qty" tone="cyan" />
        <StatTile value={data.estimated_value != null ? compactUsd(data.estimated_value) : "—"} label="Value" tone="green" />
      </Row>
      {data.priced_as_of && (
        <Body size={12} tone="muted">
          Prices as of {shortDate(data.priced_as_of)}.
        </Body>
      )}
      <Row gap={10}>
        <TextInput
          style={[inputStyle, { flex: 1 }]}
          placeholder="Search name, set or number…"
          placeholderTextColor={colors.textMuted}
          value={query}
          onChangeText={setQuery}
          autoCorrect={false}
          clearButtonMode="while-editing"
        />
        <PixelButton title="Add" icon="plus" size="sm" onPress={() => router.push("/scan/binder")} />
      </Row>
      {shown.length === 0 && <Body tone="muted">No cards match “{query}”.</Body>}
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 12 }}>
        {shown.map((c) => (
          <Panel key={c.id} style={{ width: tileW, opacity: busy === c.id ? 0.55 : 1 }} padding={8}>
            <View style={{ gap: 5 }}>
              <View>
                <CardArt uri={c.image_url} label={c.name} />
                <View style={{ position: "absolute", top: 4, right: 4 }}>
                  <Badge label={`×${c.qty}`} tone="gold" />
                </View>
              </View>
              <Body weight="black" numberOfLines={1}>
                {c.name ?? "Unknown card"}
              </Body>
              <Body size={11} tone="muted" numberOfLines={1}>
                {c.card_number ?? "—"} · {c.set_name ?? "Unknown set"}
              </Body>
              <Body style={textStyles.price}>{priceRange(c.price_usd_low, c.price_usd_high) ?? "—"}</Body>
              <Row gap={6}>
                <PixelButton title="−" size="sm" accessibilityLabel="Decrease quantity" disabled={c.qty <= 1 || busy === c.id} onPress={() => changeQty(c.id, c.qty - 1)} />
                <PixelButton title="+" size="sm" accessibilityLabel="Increase quantity" disabled={busy === c.id} onPress={() => changeQty(c.id, c.qty + 1)} />
                <View style={{ flex: 1 }} />
                <PixelButton title="✕" size="sm" variant="ghost" accessibilityLabel="Remove card" disabled={busy === c.id} onPress={() => remove(c.id)} />
              </Row>
            </View>
          </Panel>
        ))}
      </View>
    </View>
  );
}

export default function CollectionTab() {
  return (
    <Screen>
      <AppHeader />
      <ScreenTitle kicker="Card vault" title="My collection" />
      <RequireTrainer icon="chest" reason="Log in to keep a collection.">
        <Vault />
      </RequireTrainer>
    </Screen>
  );
}
