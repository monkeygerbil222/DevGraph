import Graph from "graphology";
import Sigma, { DEFAULT_STYLES } from "sigma";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import forceAtlas2 from "graphology-layout-forceatlas2";
window.SigmaBundle = { Graph, Sigma, DEFAULT_STYLES, FA2Layout, forceAtlas2 };
