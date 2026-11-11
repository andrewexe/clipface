export function Placeholder(props: { title: string; phase: string; children: string }) {
  return (
    <section>
      <h1>{props.title}</h1>
      <div className="card empty">
        <p>{props.children}</p>
        <p className="muted small">Not built yet: arrives in {props.phase}.</p>
      </div>
    </section>
  );
}
