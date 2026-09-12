import { Navigate, Route, Routes } from 'react-router-dom';
import DriverApp from './pages/DriverApp';
import OperatorDashboard from './pages/OperatorDashboard';

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<OperatorDashboard />} />
      <Route path="/driver" element={<DriverApp />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
