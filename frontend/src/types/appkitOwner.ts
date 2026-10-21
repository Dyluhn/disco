export type TweakEditor = "boolean" | "enum" | "int" | "float" | "color" | "palette" | "text";
export type TweakValue = string | number | boolean;
export interface TweakFieldView {
  key: string;
  label: string;
  editor: TweakEditor;
  options?: string[];
  colors?: string[];
  min?: number;
  max?: number;
  step?: number;
  maxLength?: number;
}
export interface AppKitOwnerControls {
  title: string;
  fields: TweakFieldView[];
  values: Record<string, TweakValue>;
  spec_digest: string;
  editable: boolean;
}
