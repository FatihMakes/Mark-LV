#include "HeroProfileSubsystem.h"
#include "HeroRulesLibrary.h"
#include "HeroSaveGame.h"
#include "Kismet/GameplayStatics.h"

bool UHeroProfileSubsystem::InitializeDefaultProfile(EHeroSpecies Species, FString& OutError)
{
	if (!UHeroRulesLibrary::IsValidSpecies(Species))
	{
		OutError = TEXT("Species must be Raccoon, Chicken, or Cat.");
		return false;
	}

	CurrentProfile = UHeroRulesLibrary::MakeDefaultProfile(Species);
	return UHeroRulesLibrary::ValidateProfile(CurrentProfile, OutError);
}

bool UHeroProfileSubsystem::SetProfile(const FHeroProfile& NewProfile, FString& OutError)
{
	if (!UHeroRulesLibrary::ValidateProfile(NewProfile, OutError))
	{
		return false;
	}

	CurrentProfile = NewProfile;
	return true;
}

bool UHeroProfileSubsystem::IsSlotIndexValid(int32 SlotIndex, FString& OutError) const
{
	if (SlotIndex < MinPresetSlot || SlotIndex > MaxPresetSlot)
	{
		OutError = FString::Printf(TEXT("Slot index out of range. Valid slots: %d-%d."), MinPresetSlot, MaxPresetSlot);
		return false;
	}
	return true;
}

bool UHeroProfileSubsystem::SaveProfile(int32 SlotIndex, FString& OutError)
{
	if (!IsSlotIndexValid(SlotIndex, OutError))
	{
		return false;
	}

	if (!UHeroRulesLibrary::ValidateProfile(CurrentProfile, OutError))
	{
		return false;
	}

	UHeroSaveGame* SaveObj = Cast<UHeroSaveGame>(UGameplayStatics::CreateSaveGameObject(UHeroSaveGame::StaticClass()));
	if (!SaveObj)
	{
		OutError = TEXT("Failed to create save object.");
		return false;
	}

	SaveObj->HeroProfile = CurrentProfile;
	if (!UGameplayStatics::SaveGameToSlot(SaveObj, MakeSlotName(SlotIndex), 0))
	{
		OutError = TEXT("Failed to save profile.");
		return false;
	}

	OutError.Reset();
	return true;
}

bool UHeroProfileSubsystem::LoadProfile(int32 SlotIndex, FString& OutError)
{
	if (!IsSlotIndexValid(SlotIndex, OutError))
	{
		return false;
	}

	if (!UGameplayStatics::DoesSaveGameExist(MakeSlotName(SlotIndex), 0))
	{
		OutError = TEXT("No save exists in that slot.");
		return false;
	}

	USaveGame* Raw = UGameplayStatics::LoadGameFromSlot(MakeSlotName(SlotIndex), 0);
	UHeroSaveGame* SaveObj = Cast<UHeroSaveGame>(Raw);
	if (!SaveObj)
	{
		OutError = TEXT("Save data is invalid.");
		return false;
	}

	if (!UHeroRulesLibrary::ValidateProfile(SaveObj->HeroProfile, OutError))
	{
		return false;
	}

	CurrentProfile = SaveObj->HeroProfile;
	OutError.Reset();
	return true;
}

bool UHeroProfileSubsystem::DoesPresetExist(int32 SlotIndex) const
{
	if (SlotIndex < MinPresetSlot || SlotIndex > MaxPresetSlot)
	{
		return false;
	}
	return UGameplayStatics::DoesSaveGameExist(MakeSlotName(SlotIndex), 0);
}

FString UHeroProfileSubsystem::MakeSlotName(int32 SlotIndex) const
{
	return FString::Printf(TEXT("HeroProfile_%d"), SlotIndex);
}
