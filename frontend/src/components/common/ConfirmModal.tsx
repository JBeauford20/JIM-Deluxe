import { Modal, Button } from 'react-bootstrap'

interface Props {
  show:       boolean
  title:      string
  body:       React.ReactNode
  confirmLabel?: string
  confirmVariant?: string
  onConfirm:  () => void
  onCancel:   () => void
  loading?:   boolean
}

export default function ConfirmModal({
  show, title, body, confirmLabel = 'Confirm',
  confirmVariant = 'cta', onConfirm, onCancel, loading
}: Props) {
  return (
    <Modal show={show} onHide={onCancel} centered backdrop="static">
      <Modal.Header closeButton>
        <Modal.Title style={{ fontSize: 16 }}>{title}</Modal.Title>
      </Modal.Header>
      <Modal.Body>{body}</Modal.Body>
      <Modal.Footer>
        <Button variant="outline-secondary" onClick={onCancel} disabled={loading}>
          Cancel
        </Button>
        <Button className={`btn btn-${confirmVariant}`} onClick={onConfirm} disabled={loading}>
          {loading
            ? <><span className="spinner-border spinner-border-sm me-2" />Working…</>
            : confirmLabel
          }
        </Button>
      </Modal.Footer>
    </Modal>
  )
}
