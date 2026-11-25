import { Background, BaseEdge, Controls, ReactFlow, MarkerType, getBezierPath, type Node, type Edge, type EdgeProps } from '@xyflow/react';
import type { AskResult, Selection } from './types';
import { colorType } from './Graph';

interface Props {
  graph: AskResult['graph'];
  selection: Selection;
  onSelect: (selection: Selection) => void;
}

type AssertionEdge = Edge<{ curveOffset: number }>;

function ReportedEdge(props: EdgeProps<AssertionEdge>) {
  const { sourceX, sourceY, targetX, targetY } = props;
  const offset = props.data?.curveOffset ?? 0;
  let [path, labelX, labelY] = getBezierPath(props);
  if (offset) {
    const dx = targetX - sourceX;
    const dy = targetY - sourceY;
    const length = Math.hypot(dx, dy) || 1;
    const controlX = (sourceX + targetX) / 2 - dy / length * offset;
    const controlY = (sourceY + targetY) / 2 + dx / length * offset;
    path = `M ${sourceX},${sourceY} Q ${controlX},${controlY} ${targetX},${targetY}`;
    labelX = (sourceX + 2 * controlX + targetX) / 4;
    labelY = (sourceY + 2 * controlY + targetY) / 4;
  }
  return <BaseEdge id={props.id} path={path} labelX={labelX} labelY={labelY} label={props.label} labelStyle={props.labelStyle} labelBgStyle={props.labelBgStyle} markerEnd={props.markerEnd} style={props.style} interactionWidth={24} />;
}

const edgeTypes = { assertion: ReportedEdge };

export default function GraphCanvas({ graph, selection, onSelect }: Props) {
  const selectedEdges = new Set(graph.edges.filter(edge =>
    selection?.kind === 'edge' ? selection.id === edge.id :
    selection?.kind === 'node' ? edge.source === selection.id || edge.target === selection.id :
    selection?.kind === 'citation' ? edge.evidence_ids.includes(selection.id) : false,
  ).map(edge => edge.id));
  const relatedNodes = new Set(graph.edges.filter(edge => selectedEdges.has(edge.id)).flatMap(edge => [edge.source, edge.target]));
  const types = [...new Set(graph.nodes.map(node => node.type))];
  const maxRows = Math.max(3, Math.ceil(Math.sqrt(graph.nodes.length)));
  const typeColumns = new Map<string, number>();
  let column = 0;
  for (const type of types) {
    typeColumns.set(type, column);
    column += Math.ceil(graph.nodes.filter(node => node.type === type).length / maxRows);
  }
  const rows = new Map<string, number>();
  const nodes: Node[] = graph.nodes.map(node => {
    const row = rows.get(node.type) ?? 0;
    rows.set(node.type, row + 1);
    const selected = relatedNodes.has(node.id) || (selection?.kind === 'node' && selection.id === node.id);
    return {
      id: node.id,
      position: { x: ((typeColumns.get(node.type) ?? 0) + Math.floor(row / maxRows)) * 290 + (row % 2) * 24, y: (row % maxRows) * 140 },
      data: { label: <><span className="flow-node-type">{node.type}</span><strong>{selected ? '✓ ' : ''}{node.label}</strong></> },
      className: `atlas-node type-${colorType(node.type)}`,
      selected,
      ariaLabel: `${node.label}, ${node.type}${selected ? ', highlighted' : ''}. Select to show passages for incident links.`,
      draggable: false,
    };
  });
  const nodeIds = new Set(graph.nodes.map(node => node.id));
  const labels = new Map(graph.nodes.map(node => [node.id, node.label]));
  // Keep assertion IDs intact and separate overlapping assertions visually.
  const pairs = new Map<string, string[]>();
  for (const edge of graph.edges) {
    const key = JSON.stringify([edge.source, edge.target].sort());
    pairs.set(key, [...(pairs.get(key) ?? []), edge.id]);
  }
  const edges: Edge[] = graph.edges.filter(edge => nodeIds.has(edge.source) && nodeIds.has(edge.target)).map(edge => {
    const selected = selectedEdges.has(edge.id);
    const siblings = pairs.get(JSON.stringify([edge.source, edge.target].sort()))!;
    const direction = edge.source <= edge.target ? 1 : -1;
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: `${selected ? '✓ ' : ''}${edge.label}`,
      type: 'assertion',
      data: { curveOffset: (siblings.indexOf(edge.id) - (siblings.length - 1) / 2) * 85 * direction },
      selected,
      focusable: true,
      interactionWidth: 24,
      ariaLabel: `${labels.get(edge.source)}: ${edge.label}: ${labels.get(edge.target)}. Select to show evidence.`,
      markerEnd: { type: MarkerType.ArrowClosed, color: selected ? '#086d67' : '#879791' },
      style: { stroke: selected ? '#086d67' : '#879791', strokeWidth: selected ? 3 : 1.5 },
      labelStyle: { fill: '#263d3b', fontSize: 11 },
      labelBgStyle: { fill: '#fcfcf8', fillOpacity: 0.96 },
    };
  });
  return (
    <ReactFlow
      key={graph.nodes.map(node => node.id).join('|')}
      nodes={nodes}
      edges={edges}
      edgeTypes={edgeTypes}
      forceColorMode="light"
      fitView
      fitViewOptions={{ padding: 0.2, maxZoom: 1.15 }}
      minZoom={0.1}
      maxZoom={2}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      onNodeClick={(_, node) => onSelect({ kind: 'node', id: node.id })}
      onEdgeClick={(_, edge) => onSelect({ kind: 'edge', id: edge.id })}
      onPaneClick={() => onSelect(null)}
      onNodesChange={changes => {
        for (const change of changes) {
          if (change.type === 'select' && change.selected) {
            onSelect({ kind: 'node', id: change.id });
            break;
          }
        }
      }}
      onEdgesChange={changes => {
        for (const change of changes) {
          if (change.type === 'select' && change.selected) {
            onSelect({ kind: 'edge', id: change.id });
            break;
          }
        }
      }}
      onKeyDown={event => { if (event.key === 'Escape') onSelect(null); }}
      aria-label="Links scoped to cited sources and passages"
    >
      <Background gap={22} size={1} color="#cdd7cf" />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}
