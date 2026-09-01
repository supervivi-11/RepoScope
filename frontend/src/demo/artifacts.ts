import { decodeDemoBundle } from "../contracts";
import rawBundle from "./cases.v1.json";

export const bundledDemos = decodeDemoBundle(rawBundle);
