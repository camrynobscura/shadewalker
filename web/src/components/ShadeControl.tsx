import { describeShadeLayers, SHADE_CHOICES, type ShadeLayers } from '../shadeLayers'
import { LayersIcon } from './icons'
import { MenuChoices, PillMenu } from './PillMenu'
import styles from './ShadeControl.module.css'

interface ShadeControlProps {
  layers: ShadeLayers
  onChange: (layers: ShadeLayers) => void
}

/** The shade pill, beside the time pill (#121): All shade, Tree shade or
 * Building shade -- which kinds
 * of shade the routes are scored by, and the route rows count. It opens
 * the same small white menu as the time pill; a pick applies and closes
 * it. The caller decides what a change does, as for the time pill: the
 * plan screen waits for FIND_ROUTE, the route screen re-routes. */
export function ShadeControl({ layers, onChange }: ShadeControlProps) {
  return (
    <PillMenu
      pill={
        <>
          <LayersIcon />
          <span className={styles.visuallyHidden}>Shade from: </span>
          {describeShadeLayers(layers)}
        </>
      }
      menuClassName={styles.menu}
    >
      <MenuChoices legend="Shade from" options={SHADE_CHOICES} value={layers} onChange={onChange} />
    </PillMenu>
  )
}
