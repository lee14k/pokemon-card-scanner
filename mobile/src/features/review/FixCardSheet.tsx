import { useEffect, useMemo, useState } from "react";
import { FlatList, KeyboardAvoidingView, Modal, Platform, Pressable, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { PackCard, SetInfo } from "@shared/types";
import { api } from "@/lib/api";
import { Body, CardArt, Loader, Panel, Pixel, PixelButton, Row, colors, inputStyle } from "@/ui/kit";

/** Pick the set + type the number; we look the card up and the user confirms. */
export function FixCardSheet({
  initial,
  onApply,
  onCancel,
}: {
  initial: PackCard;
  onApply: (fixed: PackCard) => void;
  onCancel: () => void;
}) {
  const insets = useSafeAreaInsets();
  const [sets, setSets] = useState<SetInfo[] | null>(null);
  const [setId, setSetId] = useState(initial.set_id ?? "");
  const [picking, setPicking] = useState(!initial.set_id);
  const [filter, setFilter] = useState("");
  const [number, setNumber] = useState(initial.card_number ?? "");
  const [preview, setPreview] = useState<PackCard | null>(null);
  const [status, setStatus] = useState<"idle" | "looking" | "miss" | "error">("idle");

  useEffect(() => {
    api.getSets().then(setSets).catch(() => setSets([]));
  }, []);

  const chosen = sets?.find((s) => s.set_id === setId);
  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return (sets ?? []).filter((s) => !q || s.set_name.toLowerCase().includes(q) || s.set_code?.toLowerCase().includes(q));
  }, [sets, filter]);

  const look = async () => {
    if (!setId || !number.trim()) return;
    setStatus("looking");
    setPreview(null);
    try {
      const res = await api.lookupCard(setId, number.trim());
      if (res.found && res.card) {
        setPreview({ ...res.card, row_index: initial.row_index });
        setStatus("idle");
      } else setStatus("miss");
    } catch {
      setStatus("error");
    }
  };

  return (
    <Modal visible transparent animationType="slide" onRequestClose={onCancel}>
      <KeyboardAvoidingView
        behavior={Platform.OS === "ios" ? "padding" : undefined}
        style={{ flex: 1, justifyContent: "flex-end", backgroundColor: "rgba(8,5,20,0.72)" }}
      >
        <Panel style={{ marginHorizontal: 8, marginBottom: Math.max(8, insets.bottom), maxHeight: "88%" }}>
          <View style={{ gap: 12 }}>
            <Row style={{ justifyContent: "space-between" }}>
              <Pixel size={12} tone="gold">
                Fix card
              </Pixel>
              <PixelButton title="✕" variant="ghost" size="sm" onPress={onCancel} accessibilityLabel="Close" />
            </Row>

            <Body weight="black" size={14}>
              Set
            </Body>
            {picking ? (
              <View style={{ gap: 8 }}>
                <TextInput
                  style={inputStyle}
                  placeholder="Search sets…"
                  placeholderTextColor={colors.textMuted}
                  value={filter}
                  onChangeText={setFilter}
                  autoCorrect={false}
                />
                {!sets ? (
                  <Loader small />
                ) : (
                  <FlatList
                    style={{ maxHeight: 220 }}
                    data={shown}
                    keyExtractor={(s) => s.set_id}
                    keyboardShouldPersistTaps="handled"
                    renderItem={({ item }) => (
                      <Pressable
                        onPress={() => {
                          setSetId(item.set_id);
                          setPicking(false);
                        }}
                        style={({ pressed }) => ({
                          paddingVertical: 10,
                          paddingHorizontal: 8,
                          borderBottomWidth: 2,
                          borderBottomColor: colors.surface,
                          backgroundColor: pressed || item.set_id === setId ? colors.surface : "transparent",
                        })}
                      >
                        <Body weight="bold">
                          {item.set_name} {item.set_code ? <Body tone="muted">({item.set_code})</Body> : null}
                        </Body>
                      </Pressable>
                    )}
                  />
                )}
              </View>
            ) : (
              <Pressable onPress={() => setPicking(true)} style={[inputStyle, { justifyContent: "center" }]}>
                <Body weight="bold">
                  {chosen ? `${chosen.set_name}${chosen.set_code ? ` (${chosen.set_code})` : ""}` : setId || "Pick a set…"}
                  <Body tone="muted">{"  ▾"}</Body>
                </Body>
              </Pressable>
            )}

            <Body weight="black" size={14}>
              Card number
            </Body>
            <TextInput
              style={inputStyle}
              value={number}
              onChangeText={setNumber}
              placeholder="e.g. 123/198"
              placeholderTextColor={colors.textMuted}
              autoCapitalize="characters"
              onSubmitEditing={look}
            />
            <PixelButton title="Look up" onPress={look} loading={status === "looking"} disabled={!setId || !number.trim()} />
            {status === "miss" && (
              <Body tone="danger" weight="bold">
                No card found for that set + number.
              </Body>
            )}
            {status === "error" && (
              <Body tone="danger" weight="bold">
                Lookup failed — try again.
              </Body>
            )}
            {preview && (
              <Panel padding={10} depth={3}>
                <Row gap={12} style={{ alignItems: "flex-start" }}>
                  <CardArt uri={preview.image_url} width={64} label={preview.name} />
                  <View style={{ flex: 1, gap: 6 }}>
                    <Body weight="black">{preview.name}</Body>
                    <Body tone="muted" size={13}>
                      {preview.card_number} · {preview.rarity ?? "?"}
                    </Body>
                    <PixelButton title="That's it!" variant="primary" size="sm" onPress={() => onApply(preview)} />
                  </View>
                </Row>
              </Panel>
            )}
          </View>
        </Panel>
      </KeyboardAvoidingView>
    </Modal>
  );
}
