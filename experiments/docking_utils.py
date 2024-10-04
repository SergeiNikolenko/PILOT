import contextlib
import os
import pickle
import random
import string
import subprocess
import tempfile
from collections import defaultdict

import AutoDockTools
import numpy as np
import rdkit.Chem as Chem
from meeko import MoleculePreparation, obutils
from openbabel import pybel
from posecheck import PoseCheck
from rdkit.Chem import AllChem
from vina import Vina


def save_pickle(array, path, exist_ok=True):
    if exist_ok:
        with open(path, "wb") as f:
            pickle.dump(array, f)
    else:
        if not os.path.exists(path):
            with open(path, "wb") as f:
                pickle.dump(array, f)


def write_sdf_file(sdf_path, molecules, extract_mol=False):
    w = Chem.SDWriter(str(sdf_path))
    for m in molecules:
        if extract_mol:
            if m.rdkit_mol is not None:
                w.write(m.rdkit_mol)
        else:
            if m is not None:
                w.write(m)
    w.close()


def retrieve_interactions_per_mol(interactions_df):
    """
    Get a dictionary with interaction metrics per molecule
    """
    interactions_list = [i[-1] for i in interactions_df.columns]
    interactions_dict = defaultdict(list)
    for i in range(len(interactions_df)):
        tmp_dict = defaultdict(list)
        for k, row in enumerate(interactions_df.iloc[i, :]):
            tmp_dict[interactions_list[k]].append(row)
        for key, value in tmp_dict.items():
            interactions_dict[key].append(np.sum(value))

    interactions = {
        k: {"mean": np.mean(v), "std": np.std(v)} for k, v in interactions_dict.items()
    }
    return interactions_dict, interactions


def split_list(data, num_chunks):
    chunk_size = len(data) // num_chunks
    remainder = len(data) % num_chunks
    chunks = []
    start = 0
    for i in range(num_chunks):
        chunk_end = start + chunk_size + (1 if i < remainder else 0)
        chunks.append(data[start:chunk_end])
        start = chunk_end
    return chunks


def get_random_id(length=30):
    letters = string.ascii_lowercase
    return "".join(random.choice(letters) for i in range(length))


def suppress_stdout(func):
    def wrapper(*a, **ka):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                return func(*a, **ka)

    return wrapper


class BaseDockingTask(object):

    def __init__(self, pdb_block, ligand_rdmol):
        super().__init__()
        self.pdb_block = pdb_block
        self.ligand_rdmol = ligand_rdmol

    def run(self):
        raise NotImplementedError()

    def get_results(self):
        raise NotImplementedError()


class PrepLig(object):
    def __init__(self, input_mol, mol_format):
        if mol_format == "smi":
            self.ob_mol = pybel.readstring("smi", input_mol)
        elif mol_format == "sdf":
            self.ob_mol = next(pybel.readfile(mol_format, input_mol))
        else:
            raise ValueError(f"mol_format {mol_format} not supported")

    def addH(self, polaronly=False, correctforph=True, PH=7):
        self.ob_mol.OBMol.AddHydrogens(polaronly, correctforph, PH)
        obutils.writeMolecule(self.ob_mol.OBMol, "tmp_h.sdf")

    def gen_conf(self):
        sdf_block = self.ob_mol.write("sdf")
        rdkit_mol = Chem.MolFromMolBlock(sdf_block, removeHs=False)
        AllChem.EmbedMolecule(rdkit_mol, Chem.rdDistGeom.ETKDGv3())
        self.ob_mol = pybel.readstring("sdf", Chem.MolToMolBlock(rdkit_mol))
        obutils.writeMolecule(self.ob_mol.OBMol, "conf_h.sdf")

    @suppress_stdout
    def get_pdbqt(self, lig_pdbqt=None):
        preparator = MoleculePreparation()
        preparator.prepare(self.ob_mol.OBMol)
        if lig_pdbqt is not None:
            preparator.write_pdbqt_file(lig_pdbqt)
            return
        else:
            return preparator.write_pdbqt_string()


class PrepProt(object):
    def __init__(self, pdb_file):
        self.prot = pdb_file

    def del_water(self, dry_pdb_file):  # optional
        with open(self.prot) as f:
            lines = [
                l
                for l in f.readlines()
                if l.startswith("ATOM") or l.startswith("HETATM")
            ]
            dry_lines = [l for l in lines if "HOH" not in l]

        with open(dry_pdb_file, "w") as f:
            f.write("".join(dry_lines))
        self.prot = dry_pdb_file

    def addH(self, prot_pqr):  # call pdb2pqr
        self.prot_pqr = prot_pqr
        subprocess.Popen(
            ["pdb2pqr30", "--ff=AMBER", self.prot, self.prot_pqr],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        ).communicate()

    def get_pdbqt(self, prot_pdbqt):
        prepare_receptor = os.path.join(
            AutoDockTools.__path__[0], "Utilities24/prepare_receptor4.py"
        )
        subprocess.Popen(
            ["python3", prepare_receptor, "-r", self.prot_pqr, "-o", prot_pdbqt],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        ).communicate()


class VinaDock(object):
    def __init__(self, lig_pdbqt, prot_pdbqt):
        self.lig_pdbqt = lig_pdbqt
        self.prot_pdbqt = prot_pdbqt

    def _max_min_pdb(self, pdb, buffer):
        with open(pdb, "r") as f:
            lines = [
                l
                for l in f.readlines()
                if l.startswith("ATOM") or l.startswith("HEATATM")
            ]
            xs = [float(l[31:39]) for l in lines]
            ys = [float(l[39:47]) for l in lines]
            zs = [float(l[47:55]) for l in lines]
            print(max(xs), min(xs))
            print(max(ys), min(ys))
            print(max(zs), min(zs))
            pocket_center = [
                (max(xs) + min(xs)) / 2,
                (max(ys) + min(ys)) / 2,
                (max(zs) + min(zs)) / 2,
            ]
            box_size = [
                (max(xs) - min(xs)) + buffer,
                (max(ys) - min(ys)) + buffer,
                (max(zs) - min(zs)) + buffer,
            ]
            return pocket_center, box_size

    def get_box(self, ref=None, buffer=0):
        """
        ref: reference pdb to define pocket.
        buffer: buffer size to add

        if ref is not None:
            get the max and min on x, y, z axis in ref pdb and add buffer to each dimension
        else:
            use the entire protein to define pocket
        """
        if ref is None:
            ref = self.prot_pdbqt
        self.pocket_center, self.box_size = self._max_min_pdb(ref, buffer)
        print(self.pocket_center, self.box_size)

    def dock(
        self,
        score_func="vina",
        seed=0,
        mode="dock",
        exhaustiveness=8,
        save_pose=False,
        **kwargs,
    ):  # seed=0 mean random seed
        v = Vina(sf_name=score_func, seed=seed, verbosity=0, **kwargs)
        v.set_receptor(self.prot_pdbqt)
        v.set_ligand_from_file(self.lig_pdbqt)
        v.compute_vina_maps(center=self.pocket_center, box_size=self.box_size)
        if mode == "score_only":
            score = v.score()[0]
        elif mode == "minimize":
            score = v.optimize()[0]
        elif mode == "dock":
            v.dock(exhaustiveness=exhaustiveness, n_poses=1)
            score = v.energies(n_poses=1)[0][0]
        else:
            raise ValueError

        if not save_pose:
            return score
        else:
            if mode == "score_only":
                pose = None
            elif mode == "minimize":
                tmp = tempfile.NamedTemporaryFile()
                with open(tmp.name, "w") as f:
                    v.write_pose(tmp.name, overwrite=True)
                with open(tmp.name, "r") as f:
                    pose = f.read()

            elif mode == "dock":
                pose = v.poses(n_poses=1)
            else:
                raise ValueError
            return score, pose


class VinaDockingTask(BaseDockingTask):

    @classmethod
    def from_original_data(
        cls,
        data,
        ligand_root="./data/crossdocked_pocket10",
        protein_root="./data/crossdocked",
        **kwargs,
    ):
        protein_fn = os.path.join(
            os.path.dirname(data.ligand_filename),
            os.path.basename(data.ligand_filename)[:10] + ".pdb",
        )
        protein_path = os.path.join(protein_root, protein_fn)

        ligand_path = os.path.join(ligand_root, data.ligand_filename)
        ligand_rdmol = next(iter(Chem.SDMolSupplier(ligand_path)))
        return cls(protein_path, ligand_rdmol, **kwargs)

    @classmethod
    def from_generated_mol(cls, ligand_rdmol, pdb_file, **kwargs):
        return cls(pdb_file, ligand_rdmol, **kwargs)

    def __init__(
        self,
        protein_path,
        ligand_rdmol,
        ligand_pdbqt,
        protein_pdbqt,
        tmp_dir="./tmp",
        center=None,
        size_factor=1.0,
        buffer=5.0,
        pos=None,
    ):
        super().__init__(protein_path, ligand_rdmol)
        # self.conda_env = conda_env
        self.tmp_dir = os.path.realpath(tmp_dir)
        os.makedirs(tmp_dir, exist_ok=True)

        self.ligand_pdbqt = ligand_pdbqt
        self.protein_pdbqt = protein_pdbqt

        self.task_id = get_random_id()
        self.receptor_id = self.task_id + "_receptor"
        self.ligand_id = self.task_id + "_ligand"

        self.receptor_path = protein_path
        self.ligand_path = os.path.join(self.tmp_dir, self.ligand_id + ".sdf")

        self.recon_ligand_mol = ligand_rdmol
        ligand_rdmol = Chem.AddHs(ligand_rdmol, addCoords=True)

        sdf_writer = Chem.SDWriter(self.ligand_path)
        sdf_writer.write(ligand_rdmol)
        sdf_writer.close()
        self.ligand_rdmol = ligand_rdmol

        pos = ligand_rdmol.GetConformer(0).GetPositions()
        # if pos is None:
        #    raise ValueError('pos is None')
        if center is None:
            self.center = (pos.max(0) + pos.min(0)) / 2
        else:
            self.center = center

        if size_factor is None:
            self.size_x, self.size_y, self.size_z = 20, 20, 20
        else:
            self.size_x, self.size_y, self.size_z = (
                pos.max(0) - pos.min(0)
            ) * size_factor + buffer

        self.proc = None
        self.results = None
        self.output = None
        self.error_output = None
        self.docked_sdf_path = None

    def run(self, mode="dock", exhaustiveness=8, **kwargs):
        ligand_pdbqt = self.ligand_path[:-4] + ".pdbqt"
        protein_pqr = self.receptor_path[:-4] + ".pqr"
        protein_pdbqt = self.receptor_path[:-4] + ".pdbqt"

        # lig = PrepLig(self.ligand_path, "sdf")
        # lig.get_pdbqt(ligand_pdbqt)

        # prot = PrepProt(self.receptor_path)
        # if not os.path.exists(protein_pqr):
        #     prot.addH(protein_pqr)
        # if not os.path.exists(protein_pdbqt):
        #     prot.get_pdbqt(protein_pdbqt)

        dock = VinaDock(str(self.ligand_pdbqt), str(self.protein_pdbqt))
        dock.pocket_center, dock.box_size = self.center, [
            self.size_x,
            self.size_y,
            self.size_z,
        ]
        score, pose = dock.dock(
            score_func="vina",
            mode=mode,
            exhaustiveness=exhaustiveness,
            save_pose=True,
            **kwargs,
        )
        return [{"affinity": score, "pose": pose}]

    @suppress_stdout
    def run_pose_check(self):
        pc = PoseCheck()
        pc.load_protein_from_pdb(self.receptor_path)
        # pc.load_ligands_from_sdf(self.ligand_path)
        pc.load_ligands_from_mols([self.ligand_rdmol])
        clashes = pc.calculate_clashes()
        strain = pc.calculate_strain_energy()
        return {"clashes": clashes[0], "strain": strain[0]}


# if __name__ == '__main__':
#     lig_pdbqt = 'data/lig.pdbqt'
#     mol_file = 'data/1a4k_ligand.sdf'
#     a = PrepLig(mol_file, 'sdf')
#     # mol_file = 'CC(=C)C(=O)OCCN(C)C'
#     # a = PrepLig(mol_file, 'smi')
#     a.addH()
#     a.gen_conf()
#     a.get_pdbqt(lig_pdbqt)
#
#     prot_file = 'data/1a4k_protein_chainAB.pdb'
#     prot_dry = 'data/protein_dry.pdb'
#     prot_pqr = 'data/protein.pqr'
#     prot_pdbqt = 'data/protein.pdbqt'
#     b = PrepProt(prot_file)
#     b.del_water(prot_dry)
#     b.addH(prot_pqr)
#     b.get_pdbqt(prot_pdbqt)
#
#     dock = VinaDock(lig_pdbqt, prot_pdbqt)
#     dock.get_box()
#     dock.dock()