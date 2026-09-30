import { useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { NODE_COLORS, LAYOUT_KEY } from "../../utils/constants";
import { saveJSON, stableHash } from "../../utils/helpers";

/**
 * NeuralGraph - Force-directed graph visualization
 *
 * Renders the PREPARED visual graph (positions applied by
 * buildVisualGraph in App.jsx) using the proven canvas
 * painting from the original GHOST implementation:
 * glow rings, scale-dependent labels, root emphasis,
 * and a restrained particle field around the GHOST core.
 *
 * The GHOST core particle field is a visual treatment of the
 * root node: the inner particle ring is derived from the REAL
 * nodes linked to the core (one particle per connection,
 * colored by node type). Graph data remains the source of truth.
 */
export default function NeuralGraph({
  graphData = { nodes: [], links: [] },
  selectedNode = null,
  setSelectedNode = () => {},
  onNodeClick = () => {},
  onNodeDoubleClick = () => {},
  onBackgroundClick = () => {},
  graphRef = null
}) {
  const internalGraphRef = useRef(null);
  const containerRef = useRef(null);
  const [hoveredNode, setHoveredNode] = useState(null);

  // The force-graph canvas cannot derive its size from its own
  // wrapper (the wrapper sizes itself from the canvas), so the
  // container is measured explicitly and passed as width/height.
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const element = containerRef.current;

    if (!element) return undefined;

    const updateDimensions = () => {
      setDimensions({
        width: element.clientWidth,
        height: element.clientHeight,
      });
    };

    updateDimensions();

    const observer = new ResizeObserver(updateDimensions);
    observer.observe(element);

    return () => observer.disconnect();
  }, []);

  const nodes = graphData.nodes || [];

  // Fit the whole mind into view once, when the first real
  // nodes arrive. Run twice (early + settled) so the library's
  // own initial zoom on data load cannot win the race; later
  // graph polls keep the user's zoom.
  const hasAutoFittedRef = useRef(false);

  useEffect(() => {
    if (nodes.length === 0 || hasAutoFittedRef.current) {
      return undefined;
    }

    const instance = internalGraphRef.current;

    if (!instance || typeof instance.zoomToFit !== "function") {
      return undefined;
    }

    hasAutoFittedRef.current = true;

    const fit = () => {
      const current = internalGraphRef.current;

      if (current && typeof current.zoomToFit === "function") {
        current.zoomToFit(500, 70);
      }
    };

    const timers = [setTimeout(fit, 200), setTimeout(fit, 1100)];

    return () => timers.forEach(clearTimeout);
  }, [nodes.length]);

  // Expose the graph instance through the ref owned by App.jsx
  // so its focus functions drive the same instance.
  useImperativeHandle(
    graphRef,
    () => internalGraphRef.current,
    []
  );

  const getGraphInstance = () =>
    internalGraphRef.current || (graphRef && graphRef.current);

  // Real data for the GHOST core particle field: the ids of nodes
  // actually linked to the core, resolved through the current links.
  const { ghostId, ghostNeighborColors } = useMemo(() => {
    const ghostNode = (graphData.nodes || []).find(
      (node) => node && node.type === "ghost"
    );

    if (!ghostNode) {
      return { ghostId: null, ghostNeighborColors: [] };
    }

    const nodeTypeById = new Map(
      (graphData.nodes || []).map((node) => [node.id, node.type])
    );

    const colors = [];

    (graphData.links || []).forEach((link) => {
      const sourceId =
        typeof link.source === "object" ? link.source.id : link.source;
      const targetId =
        typeof link.target === "object" ? link.target.id : link.target;

      const otherId =
        sourceId === ghostNode.id
          ? targetId
          : targetId === ghostNode.id
            ? sourceId
            : null;

      if (otherId === null || otherId === undefined) return;

      const type = nodeTypeById.get(otherId);
      const color = type && NODE_COLORS[type];

      if (color) colors.push(color);
    });

    return { ghostId: ghostNode.id, ghostNeighborColors: colors };
  }, [graphData]);

  // Keep the GHOST core particle field animating even when the
  // force engine is idle. refresh() only flags a redraw; the
  // library's own animation loop performs the actual painting.
  useEffect(() => {
    if (!ghostId) return undefined;

    let frameRequestId = 0;
    let lastPaint = 0;

    const loop = (time) => {
      if (time - lastPaint > 33) {
        lastPaint = time;
        const instance = internalGraphRef.current;
        if (instance && typeof instance.refresh === "function") {
          instance.refresh();
        }
      }
      frameRequestId = requestAnimationFrame(loop);
    };

    frameRequestId = requestAnimationFrame(loop);

    return () => cancelAnimationFrame(frameRequestId);
  }, [ghostId]);

  // --- GHOST core particle field (canvas treatment of the root node) ---
  const paintCoreParticles = (node, ctx) => {
    const time = Date.now() / 1000;

    // Inner ring: one particle per real GHOST connection,
    // colored by the connected node's type.
    const neighbors = ghostNeighborColors;
    const innerCount = Math.max(neighbors.length, 1);
    const innerRadius = 12;

    for (let i = 0; i < innerCount; i += 1) {
      const angle = (i / innerCount) * Math.PI * 2 + time * 0.05;
      const x = node.x + Math.cos(angle) * innerRadius;
      const y = node.y + Math.sin(angle) * innerRadius * 0.92;

      ctx.beginPath();
      ctx.arc(x, y, 0.8, 0, Math.PI * 2);
      ctx.fillStyle = neighbors[i] || "#8fa8b5";
      ctx.globalAlpha = 0.4;
      ctx.fill();
    }

    // Outer ring: ambient field, deterministic per core identity.
    const outerCount = 12;
    const outerRadius = 19;

    for (let i = 0; i < outerCount; i += 1) {
      const seed = stableHash(`${node.id || "ENMA"}-${i}`);
      const baseAngle =
        (i / outerCount) * Math.PI * 2 + ((seed % 100) / 100) * 0.4;
      const angle = baseAngle - time * 0.03;
      const wobble = Math.sin(time * 0.7 + (seed % 10)) * 1.4;
      const x = node.x + Math.cos(angle) * (outerRadius + wobble);
      const y = node.y + Math.sin(angle) * (outerRadius + wobble) * 0.92;
      const size = 0.4 + (seed % 4) * 0.16;
      const bright = i % 3 === 0;

      ctx.beginPath();
      ctx.arc(x, y, size, 0, Math.PI * 2);
      ctx.fillStyle = bright ? "#e6eef2" : "#7d97a5";
      ctx.globalAlpha = bright ? 0.3 : 0.18;
      ctx.fill();
    }

    ctx.globalAlpha = 1;
  };

  // --- Proven node painting (restored from the original renderer) ---
  // react-force-graph-2d passes these arguments POSITIONALLY.
  const paintNode = (node, ctx, globalScale) => {
    const color = NODE_COLORS[node.type] || NODE_COLORS.system;

    const isGhost = node.type === "ghost";
    const isRoot = Boolean(node.isRoot);
    const isSelected = Boolean(selectedNode && node.id === selectedNode.id);
    const isHovered = Boolean(hoveredNode && node.id === hoveredNode.id);

    const radius = isGhost ? 5.4 : isRoot ? 4 : 2.6;

    if (isGhost) {
      // Bright core with a restrained halo — identifiable as
      // the root, not a glowing sci-fi object.
      const halo = ctx.createRadialGradient(
        node.x,
        node.y,
        radius * 0.5,
        node.x,
        node.y,
        radius * 3.2
      );

      halo.addColorStop(0, "rgba(220,231,236,0.16)");
      halo.addColorStop(0.5, "rgba(140,170,185,0.06)");
      halo.addColorStop(1, "rgba(140,170,185,0)");

      ctx.beginPath();
      ctx.arc(node.x, node.y, radius * 3.2, 0, Math.PI * 2);
      ctx.fillStyle = halo;
      ctx.fill();

      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();

      ctx.beginPath();
      ctx.arc(node.x, node.y, radius + 2, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(220,231,236,0.35)";
      ctx.lineWidth = 0.8;
      ctx.stroke();

      paintCoreParticles(node, ctx);
    } else {
      // Quiet knowledge-graph node: soft halo + solid core.
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius * 2.4, 0, Math.PI * 2);
      ctx.fillStyle = `${color}1f`;
      ctx.fill();

      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();

      if (isRoot) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius + 2.6, 0, Math.PI * 2);
        ctx.strokeStyle = `${color}66`;
        ctx.lineWidth = 0.9;
        ctx.stroke();
      }
    }

    if (isHovered && !isGhost) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius + 3.4, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(220,231,236,0.7)";
      ctx.lineWidth = 1 / globalScale;
      ctx.stroke();
    }

    if (isSelected) {
      ctx.beginPath();
      ctx.arc(
        node.x,
        node.y,
        isGhost ? radius + 3.4 : radius + 4.6,
        0,
        Math.PI * 2
      );
      ctx.strokeStyle = "rgba(150,200,220,0.85)";
      ctx.lineWidth = 1.2 / globalScale;
      ctx.stroke();
    }

    // Labels: ghost/root always, others once zoomed in.
    const label = node.label || "";
    const shouldLabel = isGhost || isRoot || globalScale > 0.6;

    if (label && shouldLabel) {
      const fontSize =
        isRoot || isGhost ? 9.5 : Math.max(7.5, 8 / globalScale);

      ctx.font = `${fontSize}px Inter, Arial, sans-serif`;
      ctx.fillStyle =
        isRoot || isGhost
          ? "rgba(220,231,236,0.92)"
          : "rgba(160,180,192,0.72)";
      ctx.textAlign = "center";
      ctx.fillText(label, node.x, node.y + radius + 11 / globalScale);
    }
  };

  // --- Interaction handlers (positional signatures) ---
  const handleNodeClick = (node) => {
    setSelectedNode(node);
    onNodeClick(node);
  };

  const handleNodeDoubleClick = (node) => {
    onNodeDoubleClick(node);
  };

  const handleNodeHover = (node) => {
    setHoveredNode(node || null);
  };

  const handleNodeDragEnd = (node) => {
    node.fx = node.x;
    node.fy = node.y;

    const layout = {};
    nodes.forEach((graphNode) => {
      if (
        graphNode.id &&
        Number.isFinite(graphNode.x) &&
        Number.isFinite(graphNode.y)
      ) {
        layout[graphNode.id] = { x: graphNode.x, y: graphNode.y };
      }
    });
    saveJSON(LAYOUT_KEY, layout);
  };

  // --- View controls (force-graph API: zoom / zoomToFit / centerAt) ---
  const handleZoomIn = () => {
    const instance = getGraphInstance();
    if (!instance) return;
    const current = instance.zoom();
    instance.zoom(current * 1.35, 400);
  };

  const handleZoomOut = () => {
    const instance = getGraphInstance();
    if (!instance) return;
    const current = instance.zoom();
    instance.zoom(current / 1.35, 400);
  };

  const handleFitView = () => {
    const instance = getGraphInstance();
    if (instance) instance.zoomToFit(500, 60);
  };

  const handleResetView = () => {
    const instance = getGraphInstance();
    if (!instance) return;
    instance.centerAt(0, 0, 500);
    instance.zoom(1, 500);
  };

  if (nodes.length === 0) {
    return (
      <div className="neural-graph-container" ref={containerRef}>
        <div className="graph-loading">
          <span className="loading-pulse">●</span>
          INITIALIZING ENMA NEURAL MAP
        </div>
      </div>
    );
  }

  return (
    <div className="neural-graph-container" ref={containerRef}>
      {dimensions.width > 0 && dimensions.height > 0 && (
        <ForceGraph2D
          ref={internalGraphRef}
          graphData={graphData}
          width={dimensions.width}
          height={dimensions.height}
        backgroundColor="rgba(0,0,0,0)"
        nodeRelSize={3}
        // Explicit pointer hit-area: the default shadow hit
        // painting was not registering hits in this setup, so
        // the interaction layer gets a generous disc per node.
        nodePointerAreaPaint={(node, color, ctx) => {
          const hitRadius = node.type === "ghost" ? 10 : node.isRoot ? 8 : 6.5;
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x, node.y, hitRadius, 0, Math.PI * 2);
          ctx.fill();
        }}
        nodeRelSize={3}
        nodeVal={(node) => {
          if (node.type === "ghost") return 7;
          if (node.isRoot) return 4.5;
          return 2.1;
        }}
        linkColor={(link) =>
          link.type === "access"
            ? "rgba(150,170,185,0.34)"
            : "rgba(130,150,165,0.13)"
        }
        linkWidth={(link) => (link.type === "access" ? 0.9 : 0.5)}
        d3AlphaDecay={1}
        d3VelocityDecay={1}
        cooldownTicks={0}
        enableNodeDrag
        canvasPixelRatio={window.devicePixelRatio || 1}
        onNodeHover={handleNodeHover}
        onNodeClick={handleNodeClick}
        onNodeDoubleClick={handleNodeDoubleClick}
        onNodeDragEnd={handleNodeDragEnd}
        onBackgroundClick={onBackgroundClick}
        nodeCanvasObject={paintNode}
        style={{
          height: "100%",
          width: "100%",
          touchAction: "none",
        }}
        />
      )}

      {/* Graph Controls */}
      <div className="graph-controls">
        <button
          className="graph-control-btn"
          title="Zoom In"
          onClick={handleZoomIn}
        >
          +
        </button>
        <button
          className="graph-control-btn"
          title="Zoom Out"
          onClick={handleZoomOut}
        >
          -
        </button>
        <button
          className="graph-control-btn"
          title="Fit View"
          onClick={handleFitView}
        >
          ↺
        </button>
        <button
          className="graph-control-btn"
          title="Reset"
          onClick={handleResetView}
        >
          ●
        </button>
      </div>
    </div>
  );
}
